"""db.py — SQLite-хранилище Noosphere.

Контракт:
    get_db() -> sqlite3.Connection   # thread-local, WAL, row_factory=Row
    init_schema()                    # идемпотентно, вызывается при старте
    now_iso() -> str                 # UTC ISO с Z
    today_local() -> str             # локальная дата (UTC+tz_offset из конфига)

Схема — см. init_schema. Таблицы: turns, sessions, session_turns, alerts,
quota_readings, budgets, fingerprints + v1.1: queue, uploads, schedules,
turns_fts (FTS5) + v9: projects, route_log. Менять только миграциями с версией.
"""
from __future__ import annotations

import datetime as dt
import faulthandler
import json
import sqlite3
import sys
import threading
import time

from .config import load_config

_LOCAL = threading.local()

SCHEMA_VERSION = 24

# Watchdog против забытых транзакций: поток, который записал и ушёл в пул без
# commit/rollback, держит write-лок SQLite своим thread-local соединением
# НЕОГРАНИЧЕННО долго (все чужие записи падают в database is locked после
# busy_timeout). Инцидент 2026-10-30: держателя не было видно ни в lsof
# (тот же процесс), ни в стеках (поток простаивает) — только трассировка
# SQL говорит, какая запись открыла транзакцию и кто её вызвал.
_TX_MAX_AGE = 15.0
_REGISTRY_LOCK = threading.Lock()
_CONNS: list[sqlite3.Connection] = []
_TX_STATE: dict[int, dict] = {}
_WATCHDOG_STARTED = False


def _callers() -> str:
    names = []
    f = sys._getframe(2)
    while f is not None and len(names) < 8:
        names.append(f.f_code.co_name)
        f = f.f_back
    return " > ".join(names)


def _on_sql(conn: sqlite3.Connection, sql: str) -> None:
    st = _TX_STATE.get(id(conn))
    if st is None:
        return
    u = sql.lstrip()[:8].upper()
    if u.startswith(("COMMIT", "ROLLBACK")):
        st["last_sql"] = sql.lstrip()[:120]
        st["callers"] = _callers()
        st["tx_open"] = None
        return
    if u.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN")):
        if st["tx_open"] is None:
            st["tx_open"] = time.time()
        # пишем каждый оператор открытой транзакции: иначе в сообщении
        # watchdog виден один implicit BEGIN и не видно, на чём залипли
        st["thread"] = threading.current_thread().name
        st["last_sql"] = sql.lstrip()[:120]
        st["callers"] = _callers()


def _tx_watchdog() -> None:
    while True:
        time.sleep(5)
        now = time.time()
        with _REGISTRY_LOCK:
            items = [(c, _TX_STATE.get(id(c))) for c in _CONNS]
        for conn, st in items:
            if not st or st["tx_open"] is None:
                continue
            if now - st["tx_open"] <= _TX_MAX_AGE:
                continue
            print(f"[db-watchdog] транзакция потока {st['thread']} не закрыта "
                  f">{_TX_MAX_AGE:.0f} с; последняя запись: {st['last_sql']!r}; "
                  f"вызов: {st['callers']} — rollback", flush=True)
            # дамп покажет, внутри sqlite поток (медленный/блокированный
            # оператор) или простаивает (забытая транзакция)
            faulthandler.dump_traceback(all_threads=True)
            try:
                conn.rollback()
            except Exception as exc:  # noqa: BLE001 — watchdog не должен падать
                print(f"[db-watchdog] rollback не удался: {exc!r}", flush=True)
            st["tx_open"] = None

# B6 (BACKLOG-15 п.4): прогон, который никто не забрал. Статус queued — это
# состояние ДО старта задачи (_run_bg сразу ставит running), поэтому queued
# старше часа означает потерянный прогон: сервер перезапустили между созданием
# и стартом, и asyncio-задача не пережила процесс. Раньше такая запись висела
# вечно и без алерта — 6 queued, из них 2 старше 12 ч.
RUN_STALE_MIN = 60


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def today_local() -> str:
    cfg = load_config()
    tz = dt.timezone(dt.timedelta(hours=cfg.server.tz_offset_hours))
    return dt.datetime.now(tz).date().isoformat()


def get_db() -> sqlite3.Connection:
    global _WATCHDOG_STARTED
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        cfg = load_config()
        path = cfg.data_dir / "noosphere.db"
        # check_same_thread=False: watchdog обязан уметь сделать rollback
        # зависшей транзакции с чужого потока; sqlite собран в serialized-режиме,
        # параллельные C-вызовы безопасны.
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        init_schema(conn)
        conn.set_trace_callback(lambda sql, c=conn: _on_sql(c, sql))
        with _REGISTRY_LOCK:
            _CONNS.append(conn)
            _TX_STATE[id(conn)] = {"tx_open": None, "thread": "", "last_sql": "",
                                   "callers": ""}
            if not _WATCHDOG_STARTED:
                _WATCHDOG_STARTED = True
                threading.Thread(target=_tx_watchdog, daemon=True,
                                 name="db-tx-watchdog").start()
        _LOCAL.conn = conn
    return conn


def _check_schema_version(c: sqlite3.Connection) -> None:
    """Громкий отказ при чужой версии БД вместо молчаливого старта без колонок.

    БД новее кода — отказ (откатись или обнови код). БД старее кода — допустимо
    только для аддитивных изменений (новые таблицы через CREATE IF NOT EXISTS);
    ломающие изменения обязаны идти явной миграцией здесь же."""
    exists = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    if not exists:
        return
    row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if not row:
        return
    stored = int(row[0])
    if stored > SCHEMA_VERSION:
        raise RuntimeError(
            f"schema_version БД ({stored}) новее кода ({SCHEMA_VERSION}) — "
            "обнови код или восстанови БД из бэкапа")
    if stored < SCHEMA_VERSION:
        # резервная копия ДО миграции (R5 аудита r2): даунгрейд кода после
        # миграции без копии = перманентный простой под keepalive.
        # r3 (P1-r3-10): backup через sqlite3 API, а не copy2 файла — при живом
        # WAL copy2 основного файла может не содержать последние коммиты.
        try:
            src = c.execute("PRAGMA database_list").fetchone()[2]
            if src:
                bak = sqlite3.connect(f"{src}.bak-v{stored}")
                c.backup(bak)
                bak.close()
        except Exception as e:
            print(f"[db] бэкап перед миграцией не удался: {e!r}", flush=True)
        if stored < 3:
            cols = {r[1] for r in c.execute("PRAGMA table_info(turns)").fetchall()}
            if "cost_usd" not in cols:
                c.execute("ALTER TABLE turns ADD COLUMN cost_usd REAL DEFAULT 0")
        if stored < 8:
            cols = {r[1] for r in c.execute("PRAGMA table_info(sessions)").fetchall()}
            if "folder" not in cols:
                c.execute("ALTER TABLE sessions ADD COLUMN folder TEXT")
            if "pinned" not in cols:
                c.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER DEFAULT 0")
        if stored < 10:
            cols = {r[1] for r in c.execute("PRAGMA table_info(tool_audit)").fetchall()}
            if "turn_no" not in cols:
                c.execute("ALTER TABLE tool_audit ADD COLUMN turn_no INTEGER")
            if "rc" not in cols:
                c.execute("ALTER TABLE tool_audit ADD COLUMN rc INTEGER")
            if "args_summary" not in cols:
                c.execute("ALTER TABLE tool_audit ADD COLUMN args_summary TEXT")
            cols = {r[1] for r in c.execute("PRAGMA table_info(turns)").fetchall()}
            if "turn_kind" not in cols:
                # G4: «ход владельца» в приёмке — только inline/ws; у старых строк
                # вида хода нет (NULL) → они НЕ засчитываются (строгость прибора).
                c.execute("ALTER TABLE turns ADD COLUMN turn_kind TEXT")
        if stored < 11:
            cols = {r[1] for r in c.execute("PRAGMA table_info(sessions)").fetchall()}
            if "title" not in cols:
                c.execute("ALTER TABLE sessions ADD COLUMN title TEXT")
        if stored < 15:
            cols = {r[1] for r in c.execute("PRAGMA table_info(chat_verdict)").fetchall()}
            if "mode" not in cols:
                c.execute("ALTER TABLE chat_verdict ADD COLUMN mode TEXT DEFAULT 'nightly'")
        if stored < 16:
            cols = {r[1] for r in c.execute("PRAGMA table_info(vault)").fetchall()}
            if "company" not in cols:
                c.execute("ALTER TABLE vault ADD COLUMN company TEXT DEFAULT ''")
        if stored < 17:
            cols = {r[1] for r in c.execute("PRAGMA table_info(chat_verdict)").fetchall()}
            if "soft_status" not in cols:
                c.execute("ALTER TABLE chat_verdict ADD COLUMN soft_status TEXT")
                c.execute("ALTER TABLE chat_verdict ADD COLUMN soft_reason TEXT")
        if stored < 18:
            # goal 2026-10-12: прогоны доработки из окна хранят патч и его хеш
            cols = {r[1] for r in c.execute("PRAGMA table_info(runs)").fetchall()}
            if "patch" not in cols:
                c.execute("ALTER TABLE runs ADD COLUMN patch TEXT")
                c.execute("ALTER TABLE runs ADD COLUMN patch_hash TEXT")
        if stored < 21:
            # BACKLOG-15 п.3 (исключение P1-7 аудита 11.10): попытки хода не имели
            # признака принадлежности к ходу, а в строке лежали токены ПОСЛЕДНЕГО
            # вызова — 506 строк за день при 48 POST /v1/chat, максимум
            # prompt_tokens 23 427 при бюджете хода 400 000. «Сколько сжёг ход»
            # по БД не отвечалось. Аддитивно: старые строки получают turn_uid=''
            # и помечаются собственным агрегатом, чтобы дневные суммы не
            # потеряли ни токена и остались непрерывными через миграцию.
            cols = {r[1] for r in c.execute("PRAGMA table_info(turns)").fetchall()}
            for col, ddl in (("turn_uid", "TEXT"),
                             ("turn_seq", "INTEGER DEFAULT 0"),
                             ("turn_last", "INTEGER DEFAULT 0"),
                             ("turn_prompt_tokens", "INTEGER DEFAULT 0"),
                             ("turn_completion_tokens", "INTEGER DEFAULT 0"),
                             ("turn_reasoning_tokens", "INTEGER DEFAULT 0"),
                             ("turn_ms", "INTEGER DEFAULT 0")):
                if col not in cols:
                    c.execute(f"ALTER TABLE turns ADD COLUMN {col} {ddl}")
            c.execute("CREATE INDEX IF NOT EXISTS idx_turns_uid ON turns(turn_uid)")
            c.execute("UPDATE turns SET turn_last=1, turn_prompt_tokens=prompt_tokens,"
                      " turn_completion_tokens=completion_tokens,"
                      " turn_reasoning_tokens=reasoning_tokens, turn_ms=ms"
                      " WHERE COALESCE(turn_uid,'')=''")
        if stored < 24:
            # v24: структурированный отчёт прогона (progress/waiting/ready/
            # blocked/failed/dropped) вместо состояния, зашитого в текст runs.err.
            cols = {r[1] for r in c.execute("PRAGMA table_info(runs)").fetchall()}
            if "report" not in cols:
                c.execute("ALTER TABLE runs ADD COLUMN report TEXT")
        # v19: таблицы processes/process_stages/skill_state — аддитивные
        # (CREATE IF NOT EXISTS в init_schema ниже), ALTER не нужен.
        print(f"[db] миграция схемы {stored} -> {SCHEMA_VERSION}", flush=True)


def init_schema(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    if own:
        cfg = load_config()
        conn = sqlite3.connect(str(cfg.data_dir / "noosphere.db"), timeout=30)
    c = conn
    _check_schema_version(c)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

    -- Телеметрия каждого хода (краеугольный камень FOUNDING §3).
    CREATE TABLE IF NOT EXISTS turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,               -- UTC ISO
        day TEXT NOT NULL,              -- локальная дата UTC+4 (бакеты burn)
        session_id TEXT,
        user TEXT,
        task_class TEXT,
        tier_start TEXT,
        tier_used TEXT,
        model TEXT,
        channel TEXT,                   -- alibaba | ollama | kimi-cli
        ok INTEGER,                     -- прошла ли ПРОВЕРКА (не «ответил ли транспорт»)
        answered INTEGER,               -- транспорт ответил непустым
        why TEXT,
        finish TEXT,
        prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        reasoning_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        ms INTEGER DEFAULT 0,
        err TEXT,
        escalated INTEGER DEFAULT 0,
        content_filter INTEGER DEFAULT 0,  -- отказ контент-фильтра провайдера (токены сгорели)
        cost_usd REAL DEFAULT 0,      -- v3: фактическая стоимость (ингест чужих леджеров)
        turn_kind TEXT,               -- v10: inline | ws | run | schedule | queue | revive (G4)
        turn_uid TEXT,                -- v21: один у всех попыток хода (BACKLOG-15 п.3)
        turn_seq INTEGER DEFAULT 0,   -- v21: номер попытки внутри хода
        turn_last INTEGER DEFAULT 0,  -- v21: 1 на итоговой строке хода
        turn_prompt_tokens INTEGER DEFAULT 0,     -- v21: агрегат хода, не последней попытки
        turn_completion_tokens INTEGER DEFAULT 0,
        turn_reasoning_tokens INTEGER DEFAULT 0,
        turn_ms INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_turns_day ON turns(day, channel);
    CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
    CREATE INDEX IF NOT EXISTS idx_turns_uid ON turns(turn_uid);

    -- Серверные сессии (FOUNDING §2: resume, липкость, маркер при спавне).
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        user TEXT,
        channel TEXT,                   -- закреплённый канал (липкость)
        status TEXT NOT NULL DEFAULT 'active',   -- active | abandoned | closed
        phase TEXT,                     -- маркер фазы при спавне (урок mini-orchestrator)
        meta TEXT DEFAULT '{}',         -- JSON
        folder TEXT,                    -- v8: папка в списке сессий (UX-пакет)
        pinned INTEGER DEFAULT 0,       -- v8: закреплённая сессия — наверху списка
        title TEXT                      -- v11: читабельный заголовок (route/LLM/эвристика)
    );

    -- Event-sourced ходы сессии (FOUNDING §9: событие durable до мутации).
    CREATE TABLE IF NOT EXISTS session_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id),
        turn_no INTEGER NOT NULL,
        ts TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',   -- active | done | abandoned
        snapshot TEXT,                  -- JSON снапшота контекста на уровне turn'а
        tokens_spent INTEGER DEFAULT 0,
        previous_turn_id INTEGER,
        UNIQUE(session_id, turn_no)
    );

    -- v1.8 (оптимизация списка сессий, 01.11.2026): список сессий стоил O(ходов),
    -- а не O(сессий) — не было индекса под фильтр «не закрытые + сортировка по
    -- updated_at» и под выборку последнего done-снапшота сессии.
    CREATE INDEX IF NOT EXISTS idx_sessions_status_updated
        ON sessions(status, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_turns_session_status_id
        ON session_turns(session_id, status, id);

    -- Контур реакции: алерты с SLA и эскалацией (F2 аудита).
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,             -- budget | quota | treading_water | provider | reaction-test
        severity TEXT NOT NULL,         -- crit | warning | info
        text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new',   -- new | sent | acked | escalated | digested
        sent_at TEXT,
        acked_at TEXT,
        escalated_at TEXT,
        meta TEXT DEFAULT '{}'
    );

    -- Пробы квот каналов (read-only; дисциплина «неполные данные = null»).
    CREATE TABLE IF NOT EXISTS quota_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        channel TEXT NOT NULL,
        ok INTEGER NOT NULL,
        remaining REAL,                 -- NULL = неизвестно, НЕ оценка
        unit TEXT,                      -- calls | tokens | percent | NULL
        window TEXT,                    -- 5h | day | week | month | NULL
        raw TEXT                        -- JSON сырого ответа (усечённый)
    );

    -- Дневные бюджеты по каналам (атомарные per-channel счётчики, не state.json).
    CREATE TABLE IF NOT EXISTS budgets (
        day TEXT NOT NULL,
        channel TEXT NOT NULL,
        spent_tokens INTEGER DEFAULT 0,
        spent_calls INTEGER DEFAULT 0,
        warned70 INTEGER DEFAULT 0,
        warned90 INTEGER DEFAULT 0,
        PRIMARY KEY (day, channel)
    );

    -- Анти-цикл: fingerprint'ы входов для детекции повторов (agent-pulse пороги).
    CREATE TABLE IF NOT EXISTS fingerprints (
        day TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        count INTEGER DEFAULT 0,
        last_ts TEXT,
        PRIMARY KEY (day, fingerprint)
    );

    -- v1.1: серверная очередь ходов (queue — обычные, steer — приоритетные,
    -- revive — автовозобновление abandoned-сессий при старте).
    CREATE TABLE IF NOT EXISTS queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        session_id TEXT NOT NULL,
        user TEXT,
        task_class TEXT,
        tier TEXT,
        prompt TEXT,
        kind TEXT NOT NULL DEFAULT 'queue',    -- queue | steer | revive
        status TEXT NOT NULL DEFAULT 'queued', -- queued | running | done | failed | cancelled
        payload TEXT                            -- JSON полного входа хода (system, max_tokens, ...)
    );
    CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, session_id);

    -- v1.1: вложения, грузятся чанками (сырые байты по offset), не base64 в теле.
    CREATE TABLE IF NOT EXISTS uploads (
        id TEXT PRIMARY KEY,
        ts TEXT NOT NULL,
        session_id TEXT,
        name TEXT,
        mime TEXT,
        size INTEGER,
        size_received INTEGER DEFAULT 0,
        path TEXT,
        status TEXT NOT NULL DEFAULT 'init'    -- init | done
    );

    -- v1.1: headless-прогоны по расписанию (interval ИЛИ daily_time).
    CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        prompt TEXT,
        task_class TEXT,
        tier TEXT,
        interval_minutes INTEGER,
        daily_time TEXT,                        -- "HH:MM" локальное (UTC+tz_offset)
        session_id TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_run TEXT,
        next_run TEXT,
        last_status TEXT                        -- ok | err | NULL
    );

    -- v1.1: полнотекстовый поиск по ходам (prompt + text снапшотов).
    CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
        prompt, text, session_id UNINDEXED, turn_id UNINDEXED
    );

    -- v4: фоновые агентские прогоны (батч 3).
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        session_id TEXT,
        inp TEXT,                           -- JSON запроса (prompt, tier, agent…)
        status TEXT NOT NULL,               -- queued | running | done | error | canceled
        created_at TEXT,
        finished_at TEXT,
        result_text TEXT,
        err TEXT,
        patch TEXT,                         -- v18: патч прогона доработки
        patch_hash TEXT,                    -- v18: sha256[:16] патча
        report TEXT                         -- v24: progress|waiting|ready|blocked|failed|dropped
    );

    -- v5: правила и память рабочего места (батч 4).
    CREATE TABLE IF NOT EXISTS rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        text TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        ord INTEGER NOT NULL DEFAULT 100,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        note TEXT NOT NULL,
        tags TEXT
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        note, tags, mem_id UNINDEXED
    );

    -- v6: хранилище доступов контура (vault) — ssh/mcp/токены/логины/сервисы/скилы.
    CREATE TABLE IF NOT EXISTS vault (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,             -- ssh | mcp | token | login | service | skill | note
        name TEXT NOT NULL,
        host TEXT,                      -- хост/URL/эндпоинт
        login TEXT,
        secret TEXT,                    -- сам секрет (опц.) — НЕ отдаётся поиском
        secret_file TEXT,               -- или путь к файлу с секретом (канон хозяйства)
        notes TEXT,
        tags TEXT,
        company TEXT DEFAULT '',        -- v16: группировка доступов по компаниям
        updated_at TEXT,
        UNIQUE(kind, name)
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts USING fts5(
        name, host, login, notes, tags, entry_id UNINDEXED
    );

    -- v7: журнал исполнения инструментов (аудит r2, N-P0-2). Append-only,
    -- БЕЗ полного вывода и БЕЗ значений секретов: дайджесты и длины.
    -- v10 (G9): turn_no, rc, args_summary (обрезанный след БЕЗ секретов).
    CREATE TABLE IF NOT EXISTS tool_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        session_id TEXT,
        tool TEXT NOT NULL,
        args_digest TEXT,               -- sha256(args json)[:16]
        out_len INTEGER,
        out_digest TEXT,                -- sha256(output)[:16]
        note TEXT,                      -- refuse/timeout/kill и т.п.
        turn_no INTEGER,                -- v10: номер хода сессии
        rc INTEGER,                     -- v10: exit code / статус инструмента (NULL — не применимо)
        args_summary TEXT               -- v10: обрезанная команда/путь/URL без секретов
    );

    -- v10 (G1.6): подтверждение человеком операций класса A3 из агентского хода
    -- (ssh вне allowlist, git push, внешние POST/PUT/DELETE). TTL одобрения 10 мин.
    CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        session_id TEXT,
        kind TEXT NOT NULL,             -- ssh | git-push | egress-write
        summary TEXT NOT NULL,          -- что именно просит агент (для человека)
        cmd_hash TEXT NOT NULL,         -- sha256(нормализованная команда) — ключ approve
        status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | denied
        decided_at TEXT,
        decided_by TEXT
    );

    -- v10 (G4): история стартов/сбросов приёмки (раньше INSERT OR REPLACE в meta
    -- затирал прошлое). Старое meta-значение мигрирует первой строкой.
    CREATE TABLE IF NOT EXISTS acceptance_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        action TEXT NOT NULL,           -- start | reset
        actor TEXT,
        day TEXT                        -- локальная дата старта (для action=start)
    );

    -- v10 (G10): защита ингеста чужих леджеров от дублей — source+sha256(строка).
    -- Офсеты в meta остаются быстрым путём; хеш — страховка при ротации источника.
    CREATE TABLE IF NOT EXISTS ingest_seen (
        source TEXT NOT NULL,
        line_hash TEXT NOT NULL,
        ts TEXT NOT NULL,
        PRIMARY KEY (source, line_hash)
    );

    -- v9: проекты (саморастущие) и журнал маршрутизации запросов (батч 6).
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        keywords TEXT DEFAULT '',       -- через запятую, бесплатный сигнал до LLM
        auto INTEGER DEFAULT 0,         -- 1 = создан классификатором, не владельцем
        created_at TEXT
    );

    -- Каждая классификация /v1/route — строка здесь (правка пользователя —
    -- самый ценный сигнал: override=1 и чем заменили).
    CREATE TABLE IF NOT EXISTS route_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        day TEXT NOT NULL,
        session_id TEXT,
        snippet TEXT,                   -- первые ≤500 символов запроса
        kind TEXT,                      -- chat | agent | run
        project_id INTEGER,
        project_name TEXT,
        title TEXT,
        confidence REAL,
        prefilter INTEGER DEFAULT 0,    -- 1 = решён regex, без LLM
        model TEXT,
        tier TEXT,
        prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        ms INTEGER DEFAULT 0,
        err TEXT,
        override INTEGER DEFAULT 0,
        override_kind TEXT,
        override_project_id INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_route_log_day ON route_log(day);

    -- v12 (аудит успешности чатов, дизайн 2026-10-12): детерминированные признаки
    -- ходов, вердикт чата от процесса-судьи и редкие золотые метки владельца.
    CREATE TABLE IF NOT EXISTS turn_verdict (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        turn_no INTEGER NOT NULL,
        tech_ok INTEGER, tool_ok INTEGER, cost_ok INTEGER,
        verdict TEXT,                   -- success | partial | fail
        rules_version INTEGER,
        ts TEXT,
        UNIQUE(session_id, turn_no, rules_version)
    );
    CREATE TABLE IF NOT EXISTS verdict_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, session_id TEXT, turn_no INTEGER,
        rule TEXT,                      -- tech_err | tool_rc | approval_denied | ...
        evidence TEXT,
        rules_version INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_verdict_signals_session
        ON verdict_signals(session_id);
    CREATE TABLE IF NOT EXISTS chat_verdict (
        session_id TEXT PRIMARY KEY,
        status TEXT,                    -- success | partial | fail | unresolved
        reason TEXT,
        evidence TEXT,                  -- JSON: номера ходов-свидетелей
        confidence REAL,
        judge_model TEXT, judge_tier TEXT, tokens INTEGER,
        rules_agree INTEGER,            -- сошёлся ли судья с proxy-вердиктом правил
        judge_prompt_version INTEGER,
        rules_proxy TEXT,
        mode TEXT DEFAULT 'nightly',    -- v15: nightly | deep (разбор выборки по запросу)
        soft_status TEXT,               -- v17: мягкий вердикт рядом с жёстким (вариативность)
        soft_reason TEXT,
        ts TEXT, closed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS owner_marks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, session_id TEXT,
        mark TEXT,                      -- ok | notit | none (none = метка снята)
        note TEXT
    );
    -- действующая метка читается последней строкой журнала на каждый чат списка:
    -- без индекса это полный проход owner_marks на строку (02.11, приёмка владельцем).
    CREATE INDEX IF NOT EXISTS idx_owner_marks_session
        ON owner_marks(session_id, id);

    -- v13 (ночная оптимизация, дизайн 2026-10-12 §4): контракты изменений с классом
    -- риска, override стартового тира класса поверх CLASS_TIER и версии промпта судьи.
    CREATE TABLE IF NOT EXISTS audit_contracts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, target TEXT, risk TEXT,         -- risk: A1 | A2 (A3 в белый список не входит)
        diff_json TEXT, evidence_json TEXT,
        status TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|denied|applied|expired
        decided_by TEXT, decided_at TEXT, applied_at TEXT
    );
    CREATE TABLE IF NOT EXISTS class_tier_override (
        class TEXT PRIMARY KEY,
        tier TEXT, reason TEXT, ts TEXT
    );
    CREATE TABLE IF NOT EXISTS judge_prompts (
        version INTEGER PRIMARY KEY,
        text TEXT, ts TEXT
    );
    -- v16: модель под направление (класс задачи) поверх модели тира —
    -- «глм с 1С справляется плохо»: направление выбирает модель по evidence.
    CREATE TABLE IF NOT EXISTS class_model_override (
        class TEXT PRIMARY KEY,
        model TEXT, reason TEXT, ts TEXT
    );

    -- v14 (второй контур данных аудита): сессии внешних харнесов из usage_record.jsonl.
    -- В headline-цифры Ноосферы не смешиваются (source='harness', отдельная секция).
    CREATE TABLE IF NOT EXISTS harness_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT, session_id TEXT, day TEXT, model TEXT,
        requests INTEGER DEFAULT 0,
        input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
        cached_tokens INTEGER DEFAULT 0,
        tools_json TEXT, skills_json TEXT,
        lines_added INTEGER DEFAULT 0, lines_removed INTEGER DEFAULT 0,
        ts TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_harness_day ON harness_sessions(day, source);

    -- v19 (1С-контур, 2026-10-13): процессы разработки «спека → код → судья»
    -- (processes.py) и флаги включённости скилов (skills.py, дефолт enabled).
    CREATE TABLE IF NOT EXISTS processes (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,             -- dev_1c
        task TEXT NOT NULL,
        project TEXT,
        status TEXT NOT NULL,           -- queued | running | done | needs_human | error | canceled
        error TEXT,
        deploy INTEGER DEFAULT 0,       -- после ACCEPT — стадия deploy_plan + заявка A3
        max_fix_iters INTEGER DEFAULT 2,
        created_at TEXT,
        finished_at TEXT
    );
    CREATE TABLE IF NOT EXISTS process_stages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        process_id TEXT NOT NULL REFERENCES processes(id),
        seq INTEGER NOT NULL,           -- порядок внутри процесса
        stage TEXT NOT NULL,            -- spec | code | judge | deploy_plan
        role TEXT,                      -- arch | code | review (task_class телеметрии)
        channel TEXT, model TEXT,       -- фактические
        status TEXT NOT NULL,           -- running | done | error | skipped
        artifact TEXT,                  -- полный текст стадии (спека/код/вердикт)
        verdict TEXT,                   -- у judge: ACCEPT | NEEDS_FIXES
        usage_json TEXT,
        ms INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_process_stages ON process_stages(process_id, seq);
    CREATE TABLE IF NOT EXISTS skill_state (
        name TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 1
    );

    -- v20 (2026-10-28): реестр ячеек (cells.py) — принцип nodecode.ai:
    -- агент сам объявляет/подключает свои способности. Живые ячейки (скилы
    -- на диске, MCP-серверы, встроенные тулы) сканируются на месте вызова,
    -- таблица держит только объявленные агентом (kind='custom') и заметки.
    CREATE TABLE IF NOT EXISTS cells (
        name TEXT PRIMARY KEY,
        kind TEXT NOT NULL,             -- custom | note (живые kinds не тут)
        descr TEXT,
        how TEXT,                       -- как подключается/проверяется
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT,
        updated_at TEXT
    );

    -- v22 (2026-10-30): траектория хода — порт модуля server/trajectory.py из
    -- репозитория GitLab Биокарда (проект 534). Строка — один шаг хода: вызов модели
    -- (kind=model, токены, tier) или инструмента (kind=tool, rc, args_summary БЕЗ
    -- вывода). Пишет Recorder.flush одной вставкой в конце хода; читает
    -- GET /v1/sessions/{sid}/turns/{n}/trajectory. parent_seq/depth — вложенность
    -- (вызов модели внутри инструмента).
    CREATE TABLE IF NOT EXISTS turn_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        turn_no INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        parent_seq INTEGER,
        depth INTEGER NOT NULL DEFAULT 0,
        kind TEXT NOT NULL,             -- model | tool
        started_at TEXT NOT NULL,       -- UTC ISO с миллисекундами
        offset_ms INTEGER NOT NULL DEFAULT 0,   -- от начала хода
        ms INTEGER NOT NULL DEFAULT 0,
        ok INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT '',        -- ok | fail | error | refuse | running
        channel TEXT,
        model TEXT,
        tier TEXT,
        task_class TEXT,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        ttft_ms INTEGER,
        escalated INTEGER NOT NULL DEFAULT 0,
        why TEXT,
        finish TEXT,
        err TEXT,
        tool TEXT,
        args_summary TEXT,
        rc INTEGER,
        out_len INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_turn_steps_turn ON turn_steps(session_id, turn_no, seq);
    CREATE INDEX IF NOT EXISTS idx_turn_steps_created ON turn_steps(created_at);

    -- v23 (мобильный доступ, образец Hermes Mobile): профили подключения телефона
    -- и их сессии. Токен/PIN/пароль лежат ТОЛЬКО хешами (sha256 / pbkdf2-sha256),
    -- приватный ключ клиентского mTLS-сертификата не хранится вообще — только
    -- отпечаток DER для проверки предъявленного сертификата.
    CREATE TABLE IF NOT EXISTS mobile_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        server_addr TEXT NOT NULL DEFAULT '',       -- host | user@host[:port] для ssh
        auth_mode TEXT NOT NULL DEFAULT 'bearer',   -- bearer | basic | mtls
        expires_at TEXT NOT NULL DEFAULT '',        -- UTC ISO, '' = бессрочно
        basic_login TEXT NOT NULL DEFAULT '',
        basic_hash TEXT NOT NULL DEFAULT '',        -- pbkdf2-sha256 пароля профиля
        basic_salt TEXT NOT NULL DEFAULT '',
        pin_hash TEXT NOT NULL DEFAULT '',          -- '' = профиль без PIN
        pin_salt TEXT NOT NULL DEFAULT '',
        token_hash TEXT NOT NULL DEFAULT '',        -- sha256 выданного токена nsm_…
        token_hint TEXT NOT NULL DEFAULT '',        -- последние 4 символа для карточки
        token_issued_at TEXT NOT NULL DEFAULT '',
        cert_fingerprint TEXT NOT NULL DEFAULT '',  -- sha256 DER клиентского сертификата
        cert_serial TEXT NOT NULL DEFAULT '',
        cert_expires_at TEXT NOT NULL DEFAULT '',
        revoked INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_seen TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS mobile_sessions (
        id TEXT PRIMARY KEY,
        profile_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        expires_at TEXT NOT NULL DEFAULT '',
        pin_ok INTEGER NOT NULL DEFAULT 0,          -- PIN подтверждён сервером
        closed INTEGER NOT NULL DEFAULT 0,
        closed_at TEXT NOT NULL DEFAULT '',
        client TEXT NOT NULL DEFAULT '',            -- user-agent, обрезанный
        ip TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_mobile_sessions_profile
        ON mobile_sessions(profile_id, closed);
    CREATE INDEX IF NOT EXISTS idx_mobile_profiles_token
        ON mobile_profiles(token_hash);
    """)
    # Пишем schema_version только при реальном изменении: пустой INSERT держал
    # write-лок на каждой инициализации соединения, и cron-ingest ловил здесь
    # "database is locked".
    ver = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if ver is None or ver[0] != str(SCHEMA_VERSION):
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                  (str(SCHEMA_VERSION),))
    c.commit()
    if own:
        conn.close()


def index_turn(session_id: str, turn_id: int, prompt: str, text: str) -> None:
    """Индексирует ход в FTS. turn_id — session_turns.id. Повторный вызов
    (ретрай/backfill) не плодит дубли: сначала удаляем старую запись."""
    db = get_db()
    db.execute("DELETE FROM turns_fts WHERE turn_id = ?", (str(turn_id),))
    db.execute("INSERT INTO turns_fts (prompt, text, session_id, turn_id) VALUES (?,?,?,?)",
               (prompt or "", text or "", session_id, str(turn_id)))
    db.commit()


def backfill_fts() -> int:
    """Доиндексирует ходы, которых нет в FTS (старые БД до v1.1). Вызывается
    из startup. Возвращает число добавленных записей.
    G10: ходы без prompt (старая форма снапшота) индексируются по text."""
    db = get_db()
    rows = db.execute("SELECT id, session_id, snapshot FROM session_turns").fetchall()
    n = 0
    for r in rows:
        try:
            snap = json.loads(r["snapshot"] or "{}")
        except Exception:
            continue  # битый снапшот не должен ломать старт сервера
        prompt = snap.get("prompt") or ""
        text = snap.get("text") or ""
        if not prompt and not text:
            continue
        if db.execute("SELECT 1 FROM turns_fts WHERE turn_id = ?", (str(r["id"]),)).fetchone():
            continue
        db.execute("INSERT INTO turns_fts (prompt, text, session_id, turn_id) VALUES (?,?,?,?)",
                   (prompt, text, r["session_id"], str(r["id"])))
        n += 1
    db.commit()
    return n


def rotate_journals(days: int = 30) -> dict:
    """Ротация журналов (G3/G9): route_log и tool_audit старше N дней.
    Вызывается из _reaction_loop не чаще раза в сутки (метка в meta)."""
    db = get_db()
    today = today_local()
    row = db.execute("SELECT value FROM meta WHERE key='last_journal_rotation'").fetchone()
    if row and row[0] == today:
        return {}
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    cur1 = db.execute("DELETE FROM route_log WHERE ts < ?", (cutoff,))
    cur2 = db.execute("DELETE FROM tool_audit WHERE ts < ?", (cutoff,))
    cur3 = db.execute("DELETE FROM approvals WHERE ts < ? AND status != 'pending'", (cutoff,))
    # B6 (BACKLOG-15 п.4): терминальные записи очереди ходов не чистились вовсе —
    # запись от 10.10 лежала в таблице рядом с живыми, и «очередь пуста» по ней
    # было не проверить. Срок короче общего: очередь — рабочий инструмент, не журнал.
    q_cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=min(days, 7))) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    cur4 = db.execute("DELETE FROM queue WHERE ts < ? AND status IN"
                      " ('done','failed','cancelled')", (q_cutoff,))
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_journal_rotation', ?)",
               (today,))
    db.commit()
    return {"route_log": cur1.rowcount, "tool_audit": cur2.rowcount,
            "approvals": cur3.rowcount, "queue": cur4.rowcount}


def expire_stale_runs(ttl_minutes: int = RUN_STALE_MIN) -> list[dict]:
    """B6: queued-прогоны старше TTL → error с причиной + список для алерта.

    Возвращает закрытые записи, чтобы контур реакции отправил ОДИН алерт на
    пачку (вызывается каждый тик, но срабатывает только на ещё не закрытых).
    """
    c = get_db()
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=ttl_minutes)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = c.execute("SELECT id, session_id, created_at FROM runs"
                     " WHERE status='queued' AND created_at < ?", (cutoff,)).fetchall()
    out = []
    for r in rows:
        cur = c.execute("UPDATE runs SET status='error', finished_at=?, err=?"
                        " WHERE id=? AND status='queued'",
                        (now_iso(),
                         f"не забран исполнителем за {ttl_minutes} мин — прогон потерян"
                         " при перезапуске сервера", r["id"]))
        if cur.rowcount:
            out.append({"id": r["id"], "session_id": r["session_id"],
                        "created_at": r["created_at"]})
    if out:
        c.commit()
    return out


def expire_orphan_running(live_ids: set | None = None,
                          ttl_minutes: int = RUN_STALE_MIN) -> list[dict]:
    """BACKLOG-15 п.22: running-прогон, осиротевший при перезапуске сервера.

    asyncio-задача прогона (_RUN_TASKS) не переживает процесс, но строка runs
    остаётся в 'running' навсегда. Прежний признак «последний ход сессии» не
    работал: сессия общая у нескольких прогонов, ходы соседей освежали last_turn,
    и сироты висели часами (инцидент 2026-10-30: три running с 18:24). Точный
    признак — процессная живость: running жив, только пока его задача есть в
    _RUN_TASKS живого процесса; вызывающий передаёт это множество. ttl_minutes
    оставлен как страховка для задачи, которая есть в словаре, но молчит.
    """
    c = get_db()
    live_ids = live_ids or set()
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=ttl_minutes)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = c.execute(
        "SELECT r.id, r.session_id, r.created_at,"
        " (SELECT MAX(t.ts) FROM turns t"
        "   WHERE t.session_id = r.session_id AND t.ts > r.created_at) AS last_turn"
        " FROM runs r WHERE r.status='running'").fetchall()
    out = []
    for r in rows:
        last_life = max(r["created_at"] or "", r["last_turn"] or "")
        if r["id"] in live_ids and last_life >= cutoff:
            continue
        reason = ("нет задачи в живом процессе (_RUN_TASKS) — прогон осиротел"
                  " при перезапуске") if r["id"] not in live_ids else \
                 (f"нет признаков жизни {ttl_minutes}+ мин (последний ход сессии"
                  f" {r['last_turn'] or 'не записан'}) — прогон осиротел")
        cur = c.execute(
            "UPDATE runs SET status='error', finished_at=?, err=?"
            " WHERE id=? AND status='running'",
            (now_iso(), reason, r["id"]))
        if cur.rowcount:
            out.append({"id": r["id"], "session_id": r["session_id"],
                        "created_at": r["created_at"]})
    if out:
        c.commit()
    return out


def search_turns(q: str, limit: int = 20) -> list[dict]:
    """FTS5 по prompt+text ходов. snippet из text, fallback — prompt.
    Запрос оборачиваем в кавычки как фразу: пользовательский ввод не должен
    трактоваться как синтаксис FTS (иначе ':', '(' и т.п. роняли бы поиск)."""
    db = get_db()
    phrase = q.replace('"', '""')
    sql = """
    SELECT turns_fts.session_id, turns_fts.turn_id,
           snippet(turns_fts, 1, '<b>', '</b>', '…', 24) AS snip_text,
           snippet(turns_fts, 0, '<b>', '</b>', '…', 24) AS snip_prompt,
           t.ts
    FROM turns_fts
    LEFT JOIN session_turns t ON t.id = CAST(turns_fts.turn_id AS INTEGER)
    WHERE turns_fts MATCH ?
    ORDER BY t.ts DESC
    LIMIT ?
    """
    try:
        rows = db.execute(sql, (f'{{prompt text}} : "{phrase}"', limit)).fetchall()
    except sqlite3.OperationalError:
        return []  # экзотический ввод, который не спасло фразовое обёртывание
    out = []
    for r in rows:
        snip = (r["snip_text"] or "").strip() or (r["snip_prompt"] or "").strip()
        out.append({"session_id": r["session_id"], "turn_id": r["turn_id"],
                    "ts": r["ts"], "snippet": snip})
    return out
