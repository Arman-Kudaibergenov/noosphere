"""sessions.py — event-sourced сессии Noosphere (FOUNDING §9, PROTOCOL «Контракт сессий»).

Правила, которые здесь зафиксированы и нарушать нельзя:
- запись события (session_turns) durable в SQLite ДО любой мутации in-memory у вызывающего;
- снапшот контекста хранится на уровне turn'а; восстановление = чтение снапшота,
  реплей лога не делаем;
- финальный статус turn'а — first-terminal-write-wins: повторный complete/abandon — no-op;
- перед отправкой истории в LLM — close_open_tool_calls (иначе API отвергнет историю);
- при остановке сервера активные turn'ы → 'abandoned', токены сохранены;
  продолжение = новый turn от снапшота;
- при спавне сессии пишется маркерный файл {data_dir}/sessions/{sid}.phase:
  атрибуция по mtime ломается на handoff (урок mini-orchestrator).

Синхронный модуль; FastAPI вызывает из threadpool — на каждый поток своё
thread-local соединение из server.db.get_db().
"""
from __future__ import annotations

import faulthandler
import json
import logging
import re
import sqlite3
import time
import uuid
from pathlib import Path

from .config import load_config
from .db import get_db, now_iso

log = logging.getLogger(__name__)

TERMINAL_STATUSES = ("done", "abandoned")

TOOL_CALL_STUB = "[tool call interrupted: no result recorded]"


def _phase_marker_path(session_id: str) -> Path:
    return load_config().data_dir / "sessions" / f"{session_id}.phase"


def _write_phase_marker(session_id: str, phase: str | None, created_at: str,
                        user: str, channel: str) -> None:
    # Маркер пишется после durable-коммита сессии: его потеря не должна
    # откатывать уже созданную сессию, поэтому ошибка ФС — warning, не исключение.
    try:
        p = _phase_marker_path(session_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "phase": phase,
            "created_at": created_at,
            "user": user,
            "channel": channel,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("не удалось записать маркер фазы для сессии %s: %s", session_id, exc)


def create_session(user: str, channel: str, phase: str | None = None,
                   meta: dict | None = None) -> str:
    sid = str(uuid.uuid4())
    ts = now_iso()
    db = get_db()
    db.execute(
        "INSERT INTO sessions (id, created_at, updated_at, user, channel, status, phase, meta)"
        " VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        (sid, ts, ts, user, channel, phase, json.dumps(meta or {}, ensure_ascii=False)),
    )
    db.commit()
    _write_phase_marker(sid, phase, ts, user, channel)
    return sid


def append_turn(session_id: str, snapshot: dict, previous_turn_id: int | None = None) -> int:
    """Новый turn со status='active'; durable СРАЗУ, до действий вызывающего. Возвращает turn_no."""
    db = get_db()
    ts = now_iso()
    for attempt in range(3):
        try:
            # BEGIN IMMEDIATE: иначе два потока могут прочитать один max(turn_no)
            # и схлопотаться на UNIQUE(session_id, turn_no).
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT COALESCE(MAX(turn_no), 0) + 1 AS next_no FROM session_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            turn_no = int(row["next_no"])
            db.execute(
                "INSERT INTO session_turns (session_id, turn_no, ts, status, snapshot, previous_turn_id)"
                " VALUES (?, ?, ?, 'active', ?, ?)",
                (session_id, turn_no, ts, json.dumps(snapshot, ensure_ascii=False), previous_turn_id),
            )
            db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (ts, session_id))
            db.commit()
            return turn_no
        except sqlite3.OperationalError as exc:
            db.rollback()
            # "database is locked": соседний поток/процесс держит write-лок дольше
            # busy_timeout. Не роняем turn в HTTP 500 — ждём и повторяем, а при
            # первой же осечке дампим стеки всех потоков: это единственная
            # зацепка, кто именно держит лок.
            if "locked" not in str(exc) or attempt == 2:
                raise
            log.warning("append_turn: DB is locked (attempt %d/3), retrying; thread dump follows",
                        attempt + 1)
            faulthandler.dump_traceback(all_threads=True)
            time.sleep(2 * (attempt + 1))
        except Exception:
            db.rollback()
            raise
    raise sqlite3.OperationalError("database is locked")  # недостижимо


def _mask_snapshot(obj, secrets: list[str]) -> None:
    """Заменяет точные значения reveal'нутых секретов на '***' во всех строковых
    полях снапшота (G1.5): раскрытый секрет не оседает в БД/FTS."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                for s in secrets:
                    if s and s in v:
                        obj[k] = v.replace(s, "***")
                        v = obj[k]
            else:
                _mask_snapshot(v, secrets)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                for s in secrets:
                    if s and s in v:
                        obj[i] = v.replace(s, "***")
                        v = obj[i]
            else:
                _mask_snapshot(v, secrets)


def complete_turn(session_id: str, turn_no: int, tokens_spent: int,
                  snapshot: dict | None = None,
                  mask: list[str] | None = None) -> bool:
    """Финализирует turn: status='done'. Уже терминальный turn — no-op, False.
    mask — значения reveal'нутых за ход секретов (G1.5): вырезаются из снапшота
    до записи (снапшот → БД → FTS, дальше значение не растекается)."""
    if snapshot is not None and mask:
        _mask_snapshot(snapshot, mask)
    db = get_db()
    sets = "status = 'done', tokens_spent = ?"
    params: list = [tokens_spent]
    if snapshot is not None:
        sets += ", snapshot = ?"
        params.append(json.dumps(snapshot, ensure_ascii=False))
    # first-terminal-write-wins атомарно: UPDATE защёлкнут условием status='active'.
    params += [session_id, turn_no]
    cur = db.execute(
        f"UPDATE session_turns SET {sets} WHERE session_id = ? AND turn_no = ? AND status = 'active'",
        params,
    )
    if cur.rowcount == 0:
        db.rollback()
        return False
    db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now_iso(), session_id))
    db.commit()
    return True


def close_open_tool_calls(messages: list[dict]) -> list[dict]:
    """Подставляет stub-ошибки на tool_calls без ответа, не меняя порядок остальных сообщений."""
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    out: list[dict] = []
    for m in messages:
        out.append(m)
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            tc_id = tc.get("id") if isinstance(tc, dict) else None
            if tc_id and tc_id not in answered:
                out.append({"role": "tool", "tool_call_id": tc_id, "content": TOOL_CALL_STUB})
    return out


def get_history(session_id: str, max_turns: int = 20, max_chars: int = 12000) -> list[dict]:
    """История диалога для LLM: done-ходы сессии как messages user/assistant,
    в хронологическом порядке. Без неё модель не помнит контекст (P0-5 аудита
    11.10: в запрос уходило одно сообщение пользователя).
    Обрезка: последние max_turns ходов, затем по суммарной длине max_chars
    (старые ходы выкидываем первыми). Провалившийся ход остаётся в истории
    с честной пометкой — иначе модель не узнает, что вопрос уже задавался."""
    db = get_db()
    rows = db.execute(
        "SELECT snapshot FROM session_turns WHERE session_id = ? AND status = 'done'"
        " ORDER BY turn_no DESC LIMIT ?",
        (session_id, max_turns),
    ).fetchall()
    msgs: list[dict] = []
    for row in reversed(rows):
        try:
            snap = json.loads(row["snapshot"] or "{}")
        except Exception:
            continue
        prompt = snap.get("prompt")
        if not prompt:
            if snap.get("kind") == "system-note" and snap.get("text"):
                # v1.6: служебные заметки (запуск рана) — модель должна их видеть,
                # иначе отвечает «не знаю, что за ран» на только что запущенное
                msgs.append({"role": "assistant", "content": snap["text"]})
            continue
        msgs.append({"role": "user", "content": prompt})
        text = snap.get("text") or ""
        if not text:
            err = snap.get("err") or "неизвестная ошибка"
            text = f"[ход завершился ошибкой: {err}]"
        msgs.append({"role": "assistant", "content": text})
    while msgs and sum(len(m["content"]) for m in msgs) > max_chars:
        msgs = msgs[2:]  # выкидываем самый старый ход целиком (user+assistant)
    return msgs


def get_snapshot(session_id: str) -> dict | None:
    """Снапшот последнего done-turn'а, иначе последнего любого turn'а."""
    db = get_db()
    row = db.execute(
        "SELECT snapshot FROM session_turns WHERE session_id = ? AND status = 'done'"
        " ORDER BY turn_no DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if row is None:
        row = db.execute(
            "SELECT snapshot FROM session_turns WHERE session_id = ?"
            " ORDER BY turn_no DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None or row["snapshot"] is None:
        return None
    return json.loads(row["snapshot"])


def fork(session_id: str, turn_id: int) -> str:
    """Новая сессия-форк: первый turn копирует снапшот turn_id и ссылается на него."""
    db = get_db()
    turn = db.execute(
        "SELECT session_id, snapshot FROM session_turns WHERE id = ?", (turn_id,)
    ).fetchone()
    if turn is None:
        raise ValueError(f"turn id={turn_id} не найден")
    src = db.execute(
        "SELECT user, channel, phase, meta FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if src is None:
        raise ValueError(f"сессия {session_id} не найдена")
    new_sid = create_session(
        user=src["user"] or "", channel=src["channel"] or "",
        phase=src["phase"], meta=json.loads(src["meta"] or "{}"),
    )
    snapshot = json.loads(turn["snapshot"]) if turn["snapshot"] else {}
    append_turn(new_sid, snapshot, previous_turn_id=turn_id)
    return new_sid


def abandon_active_turns() -> int:
    """Все активные turn'ы → 'abandoned' (токены не трогаем — они уже записаны).

    Сессии без ни одного done-turn → status='abandoned'.
    Возвращает число turn'ов, помеченных abandoned.
    Вызывается из SIGTERM/atexit-обработчика (живёт в другом модуле).
    """
    db = get_db()
    ts = now_iso()
    cur = db.execute(
        "UPDATE session_turns SET status = 'abandoned' WHERE status = 'active'"
    )
    marked = cur.rowcount
    db.execute(
        "UPDATE sessions SET status = 'abandoned', updated_at = ?"
        " WHERE status = 'active' AND NOT EXISTS ("
        "   SELECT 1 FROM session_turns t WHERE t.session_id = sessions.id AND t.status = 'done')",
        (ts,),
    )
    db.commit()
    return marked


def resume(session_id: str) -> dict:
    """Реанкоринг: abandoned-сессия → 'active', новый turn от последнего снапшота."""
    db = get_db()
    snapshot = get_snapshot(session_id)
    db.execute(
        "UPDATE sessions SET status = 'active', updated_at = ? WHERE id = ? AND status = 'abandoned'",
        (now_iso(), session_id),
    )
    db.commit()
    turn_no = append_turn(session_id, snapshot or {})
    return {"session_id": session_id, "turn_no": turn_no, "snapshot": snapshot}


def set_channel(session_id: str, channel: str) -> None:
    """Канал, которым сессия фактически ходила последний раз.

    Колонка sessions.channel в схеме подписана «закреплённый канал (липкость)»,
    но писалась только при создании сессии и с тех пор не обновлялась: на 29.10
    19 сессий числились на qwencloud — канале, выключенном 21.10. Для диагностики
    маршрутизации и для любой будущей липкости колонка обязана говорить правду
    (доводка E1/E6, аудит 2026-10-29)."""
    if not channel:
        return
    db = get_db()
    db.execute("UPDATE sessions SET channel = ? WHERE id = ?", (channel, session_id))
    db.commit()


def archive(session_id: str) -> bool:
    """Владелец убрал сессию из списка (status='closed'). Только «не показывать»:
    открытие по id и история не трогаются (порт из канона 29.10.2026)."""
    db = get_db()
    cur = db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (session_id,))
    db.commit()
    return cur.rowcount > 0


def meta_flag(session_id: str, key: str) -> bool:
    """Признак из sessions.meta: True, если ключ установлен и не пустой.

    None/битый JSON читаются как «признака нет» — сессия не должна ломать ход.
    """
    row = get_db().execute("SELECT meta FROM sessions WHERE id = ?",
                           (session_id,)).fetchone()
    try:
        m = json.loads(row["meta"] or "{}") if row else {}
    except ValueError:
        return False
    return bool(isinstance(m, dict) and m.get(key))


def set_meta(session_id: str, patch: dict) -> bool:
    """Слить ключи в sessions.meta (JSON-колонка есть с v1 и до батча 20 была пуста).

    Без миграции схемы: признак пробной сессии (BACKLOG-15 п.5) и липкость
    каталога скилов (п.7) — свойства сессии, а не отдельные таблицы. False —
    сессии с таким id нет.
    """
    db = get_db()
    row = db.execute("SELECT meta FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        return False
    try:
        cur = json.loads(row["meta"] or "{}")
        if not isinstance(cur, dict):
            cur = {}
    except ValueError:
        cur = {}
    cur.update(patch)
    db.execute("UPDATE sessions SET meta = ?, updated_at = ? WHERE id = ?",
               (json.dumps(cur, ensure_ascii=False), now_iso(), session_id))
    db.commit()
    return True


def _session_row_to_dict(row: sqlite3.Row) -> dict:
    keys = row.keys()
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "user": row["user"],
        "channel": row["channel"],
        "status": row["status"],
        "phase": row["phase"],
        "meta": json.loads(row["meta"] or "{}"),
        "folder": row["folder"] if "folder" in keys else None,
        "pinned": bool(row["pinned"]) if "pinned" in keys else False,
        "title": row["title"] if "title" in keys else None,
    }


def _gold_mark(mark) -> str:
    """Действующая метка владельца для клиента: '' | 'ok' | 'notit'.

    Журнал `owner_marks` хранит и строку `none` (метка снята) — клиенту она не
    отдаётся как состояние, иначе строка чата показывала бы «снято» как метку.
    """
    return mark if mark in ("ok", "notit") else ""


def set_folder(session_id: str, folder: str) -> bool:
    db = get_db()
    cur = db.execute("UPDATE sessions SET folder=? WHERE id=?",
                     (folder or None, session_id))
    db.commit()
    return cur.rowcount > 0


TITLE_MAX = 120


def sanitize_title(title: str | None) -> str:
    """Одна строка ≤120 зн: управляющие символы → пробел, пробелы схлопнуты."""
    if not title:
        return ""
    t = re.sub(r"[\x00-\x1f\x7f]+", " ", str(title))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:TITLE_MAX].strip()


def heuristic_title(prompt: str) -> str:
    """Заголовок без LLM: первые ~60 символов промпта с обрезкой по слову."""
    t = sanitize_title(prompt)
    if len(t) <= 60:
        return t
    cut = t[:60].rsplit(" ", 1)[0].strip()
    return (cut or t[:60].strip()) + "…"


def set_title(session_id: str, title: str) -> bool:
    """Явная установка заголовка (rename / доверенный title клиента).
    Пустой после sanitize заголовок не пишется."""
    t = sanitize_title(title)
    if not t:
        return False
    db = get_db()
    cur = db.execute("UPDATE sessions SET title=? WHERE id=?", (t, session_id))
    db.commit()
    return cur.rowcount > 0


def set_title_if_empty(session_id: str, title: str) -> bool:
    """Заголовок от классификатора/генератора не перекрывает ручной."""
    t = sanitize_title(title)
    if not t:
        return False
    db = get_db()
    cur = db.execute(
        "UPDATE sessions SET title=? WHERE id=? AND (title IS NULL OR title='')",
        (t, session_id))
    db.commit()
    return cur.rowcount > 0


def backfill_titles() -> int:
    """Стартовый бэкфилл без LLM: сессии без title → эвристика от первого
    user-промпта (snapshot первого хода). Идемпотентно, один раз в lifespan."""
    db = get_db()
    rows = db.execute(
        "SELECT id FROM sessions WHERE title IS NULL OR title=''").fetchall()
    n = 0
    for r in rows:
        t = db.execute(
            "SELECT snapshot FROM session_turns WHERE session_id=?"
            " ORDER BY turn_no LIMIT 1", (r["id"],)).fetchone()
        prompt = ""
        if t and t["snapshot"]:
            try:
                prompt = json.loads(t["snapshot"]).get("prompt") or ""
            except Exception:
                prompt = ""
        if prompt:
            db.execute("UPDATE sessions SET title=? WHERE id=?",
                       (heuristic_title(prompt), r["id"]))
            n += 1
    if n:
        db.commit()
    return n


def toggle_pin(session_id: str) -> bool | None:
    """Переключить закрепление; возвращает новое состояние или None (нет сессии)."""
    db = get_db()
    row = db.execute("SELECT pinned FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        return None
    new = 0 if row["pinned"] else 1
    db.execute("UPDATE sessions SET pinned=? WHERE id=?", (new, session_id))
    db.commit()
    return bool(new)


def get_turn_id(session_id: str, turn_no: int) -> int | None:
    """id строки session_turns (нужен как ключ FTS-индекса)."""
    row = get_db().execute(
        "SELECT id FROM session_turns WHERE session_id = ? AND turn_no = ?",
        (session_id, turn_no),
    ).fetchone()
    return row["id"] if row else None


_LIKE_ESC = {"\\": "\\\\", "%": "\\%", "_": "\\_"}


def _like_escape(text: str) -> str:
    """Экранирует «%» и «_»: в q это литералы, а не шаблон LIKE.

    Без экранирования запрос «%» отдавал весь список, а «_» — почти весь.
    """
    return "".join(_LIKE_ESC.get(ch, ch) for ch in text)


def _casefold_fn(db: sqlite3.Connection) -> None:
    """Регистронезависимый поиск по-человечески: LIKE в SQLite складывает регистр
    только для ASCII (ICU не собран), а заголовки у нас русские — поэтому сравнение
    идёт через casefold-UDF. Регистрация идемпотентна и стоит микросекунды."""
    try:
        db.create_function(
            "nf_casefold", 1,
            lambda s: ("" if s is None else str(s)).casefold(),
            deterministic=True)
    except (AttributeError, sqlite3.Error, TypeError):
        pass


def _session_where(q: str | None, since: str | None) -> tuple[str, list]:
    """WHERE по sessions (алиас s) + параметры: не архивные, изменённые после since,
    совпадающие с q по title или id."""
    where = ["s.status != 'closed'"]
    params: list = []
    since = (since or "").strip()
    if since:
        where.append("s.updated_at > ?")
        params.append(since)
    qs = (q or "").strip()
    if qs:
        pat = f"%{_like_escape(qs.casefold())}%"
        where.append("(nf_casefold(s.id) LIKE ? ESCAPE '\\'"
                     " OR nf_casefold(IFNULL(s.title, '')) LIKE ? ESCAPE '\\')")
        params += [pat, pat]
    return " AND ".join(where), params


def archived_ids(q: str | None = None) -> list[str]:
    """id закрытых (status='closed') сессий под тем же фильтром q.

    Нужно клиенту: архив не меняет updated_at, поэтому инкрементальный опрос
    «с прошлого раза» об архивации не узнаёт — сервер отдаёт эти id отдельно,
    и клиент вычищает их из своего списка.
    """
    db = get_db()
    _casefold_fn(db)
    where = ["s.status = 'closed'"]
    params: list = []
    qs = (q or "").strip()
    if qs:
        pat = f"%{_like_escape(qs.casefold())}%"
        where.append("(nf_casefold(s.id) LIKE ? ESCAPE '\\'"
                     " OR nf_casefold(IFNULL(s.title, '')) LIKE ? ESCAPE '\\')")
        params += [pat, pat]
    rows = db.execute(
        "SELECT s.id FROM sessions s WHERE " + " AND ".join(where) +
        " ORDER BY s.updated_at DESC", params).fetchall()
    return [r["id"] for r in rows]


def list_sessions(busy: set[str] | None = None, limit: int | None = None,
                  offset: int = 0, q: str | None = None, since: str | None = None,
                  with_meta: bool = False):
    """Список сессий + state (busy|ready|error|idle) и queue_len (PROTOCOL v1.1).
    `busy` — множество session_id с идущим ходом; передаётся из main, т.к.
    in-memory BUSY живёт там.

    Без аргументов ответ прежний (обратная совместимость): все неархивные сессии,
    поля id, created_at, updated_at, user, channel, status, phase, meta, folder,
    pinned, title, queue_len, state; плюс turns_count (int), last_turn_at
    (str|None — ts последнего хода) и owner_mark ('' | ok | notit — действующая
    золотая метка владельца, приёмка результата).

    Необязательные (v1.8, оптимизация 01.11.2026):
        limit/offset — страница выборки (limit=None — без лимита);
        q            — фильтр по title и id, регистронезависимый, % и _ литеральны;
        since        — только строки с updated_at > since (инкрементальный опрос);
        with_meta    — вернуть (список, meta), meta = {total, ids, removed}:
                       total — сколько всего под фильтром, ids — все id под
                       фильтром (без страницы), removed — id закрытых сессий под
                       текущим q (см. archived_ids).

    Стоимость O(сессий на странице), а не O(ходов): последний done-снапшот и
    счётчики берутся коррелятными подзапросами по индексам
    idx_turns_session_status_id / idx_sessions_status_updated — полного прохода
    «ORDER BY id по всем ходам всех сессий» больше нет.
    """
    db = get_db()
    _casefold_fn(db)
    where_sql, params = _session_where(q, since)
    lim = -1 if limit is None else max(0, int(limit))
    off = max(0, int(offset or 0))
    rows = db.execute(
        "SELECT s.id, s.created_at, s.updated_at, s.user, s.channel, s.status,"
        " s.phase, s.meta, s.folder, s.pinned, s.title,"
        " (SELECT COUNT(*) FROM session_turns t WHERE t.session_id = s.id)"
        "   AS turns_count,"
        " (SELECT t.ts FROM session_turns t WHERE t.session_id = s.id"
        "   ORDER BY t.id DESC LIMIT 1) AS last_turn_at,"
        " (SELECT m.mark FROM owner_marks m WHERE m.session_id = s.id"
        "   ORDER BY m.id DESC LIMIT 1) AS owner_mark,"
        " (SELECT t.snapshot FROM session_turns t"
        "   WHERE t.session_id = s.id AND t.status = 'done'"
        "   ORDER BY t.id DESC LIMIT 1) AS last_done_snapshot"
        " FROM sessions s WHERE " + where_sql +
        " ORDER BY s.updated_at DESC, s.id DESC LIMIT ? OFFSET ?",
        params + [lim, off]).fetchall()
    busy = busy or set()
    qlens = {r["session_id"]: r["n"] for r in db.execute(
        "SELECT session_id, COUNT(*) AS n FROM queue"
        " WHERE status IN ('queued','running') GROUP BY session_id")}
    out = []
    for row in rows:
        d = _session_row_to_dict(row)
        q_len = qlens.get(row["id"], 0)
        d["queue_len"] = q_len
        d["turns_count"] = int(row["turns_count"] or 0)
        d["last_turn_at"] = row["last_turn_at"]
        d["owner_mark"] = _gold_mark(row["owner_mark"])
        if row["id"] in busy or q_len:
            state = "busy"
        else:
            ok = None
            snap = row["last_done_snapshot"]
            if snap:
                try:
                    ok = json.loads(snap).get("ok")
                except Exception:
                    ok = None
            state = "error" if ok is False else ("ready" if ok is True else "idle")
        d["state"] = state
        out.append(d)
    if not with_meta:
        return out
    ids = [r["id"] for r in db.execute(
        "SELECT s.id FROM sessions s WHERE " + where_sql +
        " ORDER BY s.updated_at DESC, s.id DESC", params).fetchall()]
    total = db.execute("SELECT COUNT(*) AS n FROM sessions s WHERE " + where_sql,
                       params).fetchone()["n"]
    meta = {"total": int(total or 0), "ids": ids, "removed": archived_ids(q)}
    return out, meta


def get_session(session_id: str) -> dict | None:
    """Сессия + ходы (turn_no, status, tokens_spent, ts; id — для fork/resume)."""
    db = get_db()
    row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    turns = db.execute(
        "SELECT id, turn_no, status, tokens_spent, ts, snapshot FROM session_turns"
        " WHERE session_id = ? ORDER BY turn_no",
        (session_id,),
    ).fetchall()
    result = _session_row_to_dict(row)
    mk = db.execute(
        "SELECT mark, ts FROM owner_marks WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (session_id,)).fetchone()
    result["owner_mark"] = _gold_mark(mk["mark"] if mk else None)
    result["owner_mark_ts"] = mk["ts"] if (mk and result["owner_mark"]) else None
    out = []
    for t in turns:
        d = dict(t)
        try:
            d["snapshot"] = json.loads(d["snapshot"]) if d["snapshot"] else {}
        except Exception:
            d["snapshot"] = {}
        out.append(d)
    result["turns"] = out
    return result
