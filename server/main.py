"""main.py — FastAPI-приложение Noosphere (PROTOCOL.md v1 + v1.1).

Запуск: uvicorn server.main:app --host ... --port ...
Планировщик контура реакции (check_sla/flush_digest) — голый код, БЕЗ LLM.
При остановке активные turn'ы → abandoned (контракт сессий).
v1.1: BUSY-набор сессий, очередь/steer/revive (queue.py), FTS-поиск,
вложения чанками (uploads.py), расписания (scheduler.py).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

from . import __version__, alerts, antiloop, audit_report, burn, chat_judge, \
    db, devexec, devruns, harness_usage, mobile, optimizer, panel, pool, processes, \
    projects, push, queue, quotas, route_intent, rules, run_quality, runs, \
    scheduler, sessions, skills, tools, uploads, trajectory, verdicts, voice
from .config import load_config
from .db import init_schema, now_iso

log = logging.getLogger(__name__)

_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def _stamp_logs() -> None:
    """Метки времени в журнал сервера (BACKLOG-15 п.2, аудит 2026-10-29).

    uvicorn настраивает логирование до импорта приложения и в своём формате не
    печатает время. Строка вида `INFO:     192.168.0.216:60137 - "GET /v1/sessions
    HTTP/1.1" 200 OK` не позволяла измерить темп запросов по окну: measure.sh
    считал строки по всему файлу, поэтому дневной агрегат не показывал эффект
    правок клиента (116 GET на чат при чистом замере 24 запроса за 120 с).
    Существующий формат не переписываем, а дописываем asctime в начало — ни одно
    поле (levelprefix, client_addr, request_line, status_code) не теряется.
    Рукопожатие WebSocket в access-лог не попадает вовсе (это не HTTP-запрос),
    поэтому connect/disconnect/reject логируются явно в обработчиках ниже.
    """
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            f = h.formatter
            fmt = getattr(f, "_fmt", "") or ""
            if not fmt or fmt.startswith("%(asctime)s"):
                continue
            try:
                h.setFormatter(type(f)(f"%(asctime)s {fmt}", datefmt=_LOG_DATEFMT,
                                       use_colors=getattr(f, "use_colors", False)))
            except TypeError:      # форматтер не uvicorn-овский: берём обычный
                h.setFormatter(logging.Formatter(f"%(asctime)s {fmt}",
                                                 datefmt=_LOG_DATEFMT))
    # свой журнал приложения: print() остаётся, но log.info из любого модуля
    # пакета server теперь тоже попадает в logs/server-*.log с тем же временем
    pkg = logging.getLogger(__package__ or "server")
    if not pkg.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt=_LOG_DATEFMT))
        pkg.addHandler(h)
    pkg.setLevel(logging.INFO)
    pkg.propagate = False


_stamp_logs()

_scheduler_task: asyncio.Task | None = None
_queue_task: asyncio.Task | None = None
_devrun_task: asyncio.Task | None = None
_route_analysis_task: asyncio.Task | None = None
_audit_task: asyncio.Task | None = None


def _fail_closed_check(cfg) -> None:
    """G1.4: fail-closed _auth. Пустой токен допустим на loopback или при
    явном lan_no_auth; иначе non-loopback host обязан отказаться стартовать."""
    host = (cfg.server.host or "").strip()
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not cfg.server.token and not loopback and not getattr(cfg.server, "lan_no_auth", False):
        raise RuntimeError(
            f"отказ старта: server.token пуст при host={host!r} (не loopback) — "
            "задай token_file (~/.secrets/noosphere.token) или token в конфиге")
    if cfg.server.token and cfg.server.token_source == "inline":
        print("[auth] ВНИМАНИЕ: токен лежит инлайн в config.yaml — переходный "
              "режим; перенеси в server.token_file (вне дерева деплоя)", flush=True)

# session_id с идущим прямо сейчас ходом. Модульный, не в БД: занятость —
# свойство процесса, при рестарте она обнуляется сама, а очередь — durable.
BUSY: set[str] = set()


def _revive_abandoned() -> int:
    """Автовозобновление при старте: abandoned-сессии моложе 24ч, у которых
    последний ход — prompt без ответа, возвращаются в active и встают в
    очередь kind='revive'. Один раз на сессию (антидубль — has_active_revive)."""
    dbh = db.get_db()
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = dbh.execute(
        "SELECT * FROM sessions WHERE status = 'abandoned' AND updated_at >= ?",
        (cutoff,),
    ).fetchall()
    n = 0
    for s in rows:
        last = dbh.execute(
            "SELECT snapshot FROM session_turns WHERE session_id = ?"
            " ORDER BY turn_no DESC LIMIT 1", (s["id"],),
        ).fetchone()
        try:
            snap = json.loads(last["snapshot"]) if last and last["snapshot"] else {}
        except Exception:
            snap = {}
        if not snap.get("prompt") or snap.get("text"):
            continue
        if queue.has_active_revive(s["id"]):
            continue
        dbh.execute("UPDATE sessions SET status = 'active', updated_at = ? WHERE id = ?",
                    (now_iso(), s["id"]))
        dbh.commit()
        inp = {"session_id": s["id"], "user": s["user"] or load_config().owner_user,
               "task_class": snap.get("task_class") or "chat",
               "prompt": snap["prompt"], "system": snap.get("system"), "tier": None}
        queue.enqueue(s["id"], inp, "revive")
        n += 1
    return n


async def _reaction_loop() -> None:
    """Тик контура реакции: SLA-эскалация + дайджест + due-расписания +
    ротация журналов (раз в сутки, G3/G9). Без LLM."""
    while True:
        try:
            await asyncio.to_thread(alerts.check_sla)
            await asyncio.to_thread(alerts.flush_digest)
            await _run_due_schedules()
            rotated = await asyncio.to_thread(db.rotate_journals)
            if rotated:
                print(f"journal rotation: {rotated}")
            # B6 (BACKLOG-15 п.4): зависший в queued прогон больше не молчит
            stale = await asyncio.to_thread(db.expire_stale_runs)
            if stale:
                ids = ", ".join(str(x["id"])[:12] for x in stale)
                log.info("stale runs closed as error: %s", ids)
                await asyncio.to_thread(
                    alerts.send, "warning", "run_stale",
                    f"прогоны не забраны исполнителем и закрыты как error: {ids}"
                    f" (всего {len(stale)}, порог {db.RUN_STALE_MIN} мин)")
            # BACKLOG-15 п.22: running-сироты перезапуска больше не висят вечно
            orph = await asyncio.to_thread(db.expire_orphan_running,
                                           set(_RUN_TASKS))
            if orph:
                ids = ", ".join(str(x["id"])[:12] for x in orph)
                log.info("orphan running runs closed as error: %s", ids)
                await asyncio.to_thread(
                    alerts.send, "warning", "run_orphan",
                    f"running-прогоны без задачи в живом процессе закрыты как"
                    f" error: {ids} (всего {len(orph)})")
        except Exception as e:  # контур реакции не должен умирать молча
            print(f"reaction loop error: {e!r}")
        await asyncio.sleep(60)


async def _run_due_schedules() -> None:
    """Исполняет наступившие расписания: ход в целевую сессию (или новую
    на прогон). Провал — warning в контур реакции, цикл не роняем."""
    from . import router

    for sch in await asyncio.to_thread(scheduler.due):
        ivl, dtime = sch.get("interval_minutes"), sch.get("daily_time")
        try:
            sid = sch.get("session_id")
            if not sid:
                tier = sch.get("tier") or router.start_tier(sch.get("task_class") or "chat")
                channel = router.channel_of_tier(tier)[0]
                sid = await asyncio.to_thread(
                    sessions.create_session, "scheduler", channel, f"schedule:{sch['name']}")
            inp = {"session_id": sid, "user": "scheduler",
                   "task_class": sch.get("task_class") or "chat",
                   "prompt": sch["prompt"], "tier": sch.get("tier"),
                   "agent": False}  # headless-прогон по расписанию — без инструментов, как раньше
            if sid in BUSY:
                # сессия занята — не теряем прогон, а ставим в её очередь
                await asyncio.to_thread(queue.enqueue, sid, inp, "queue")
                await asyncio.to_thread(scheduler.mark_run, sch["id"], True, ivl, dtime)
                continue
            BUSY.add(sid)
            try:
                res = await _run_turn(sid, inp, "schedule")
            finally:
                BUSY.discard(sid)
            ok = bool(res.get("ok"))
            await asyncio.to_thread(scheduler.mark_run, sch["id"], ok, ivl, dtime)
            if not ok:
                await asyncio.to_thread(
                    alerts.send, "warning", "scheduler",
                    f"расписание «{sch['name']}» (#{sch['id']}): {res.get('err', 'провал')}")
        except Exception as e:
            print(f"schedule #{sch.get('id')} error: {e!r}")
            await asyncio.to_thread(scheduler.mark_run, sch["id"], False, ivl, dtime)
            await asyncio.to_thread(
                alerts.send, "warning", "scheduler",
                f"расписание «{sch.get('name')}» (#{sch.get('id')}): исключение {e!r}")


async def _queue_loop() -> None:
    """Разбирает очередь: головы свободных сессий → running → _run_turn.
    Ошибки одной записи не роняют цикл и соседние сессии."""
    while True:
        try:
            for item in await asyncio.to_thread(queue.heads):
                sid = item["session_id"]
                if sid in BUSY:
                    continue
                BUSY.add(sid)
                # защёлка expect='queued': если запись успели отменить — пропускаем
                if not await asyncio.to_thread(queue.set_status, item["id"], "running", "queued"):
                    BUSY.discard(sid)
                    continue
                asyncio.create_task(_exec_queued(item))
        except Exception as e:
            print(f"queue loop error: {e!r}")
        await asyncio.sleep(2)


async def _exec_queued(item: dict) -> None:
    sid = item["session_id"]
    try:
        inp = json.loads(item["payload"] or "{}")
        inp.setdefault("user", item["user"] or load_config().owner_user)
        inp.setdefault("task_class", item["task_class"] or "chat")
        inp.setdefault("prompt", item["prompt"] or "")
        res = await _run_turn(sid, inp, item["kind"])
        await asyncio.to_thread(queue.set_status, item["id"],
                                "done" if res.get("ok") else "failed")
    except Exception as e:
        print(f"queued turn #{item['id']} failed: {e!r}")
        await asyncio.to_thread(queue.set_status, item["id"], "failed")
    finally:
        BUSY.discard(sid)


async def _route_analysis_loop() -> None:
    """Ночной анализатор (батч 6): раз в час проверяет, пора ли гнать разбор
    route_log + качества ранов; итог остаётся в UI без дублирования в TG.
    Авто-применения предложений НЕТ — только текст. Ошибки не роняют сервер."""
    while True:
        try:
            if await asyncio.to_thread(run_quality.should_run_today):
                summary = await asyncio.to_thread(run_quality.run_analysis)
                await asyncio.to_thread(run_quality.mark_run_today)
                await asyncio.to_thread(
                    alerts.send, "warning", "route_analysis", summary)
        except Exception as e:  # анализатор — не критический контур
            print(f"route analysis loop error: {e!r}")
        await asyncio.sleep(3600)


async def _audit_loop() -> None:
    """Еженощный аудит успешности чатов (дизайн 2026-10-12): процесс-судья ставит
    финальные статусы закрытых чатов, итог — info-запись в UI без TG-дайджеста.
    Авто-применения нет; ошибки не роняют сервер, бюджет ночи ограничен капом
    в chat_judge.nightly."""
    while True:
        try:
            cfg = load_config()
            local = dt.datetime.now(
                dt.timezone(dt.timedelta(hours=cfg.server.tz_offset_hours)))
            # >=, а не ==: если судья ночью лёг, день не закрыт (chat_judge.nightly
            # не помечает all-proxy сутки) — следующий час повторит аудит
            if (local.hour >= cfg.route.analysis_hour
                    and await asyncio.to_thread(chat_judge.should_run_today)):
                summary = await chat_judge.nightly()
                if summary:
                    sev = "warning" if "судья недоступен для" in summary else "info"
                    await asyncio.to_thread(alerts.send, sev, "chat_audit", summary)
        except Exception as e:  # аудит — не критический контур
            print(f"chat audit loop error: {e!r}")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler_task, _queue_task, _route_analysis_task, _audit_task, \
        _devrun_task
    cfg = load_config()
    _fail_closed_check(cfg)  # G1.4: пустой токен + non-loopback → отказ старта
    init_schema()
    seeded = await asyncio.to_thread(projects.seed_defaults)
    if seeded:
        print(f"startup: projects seed — {seeded} проектов")
    n = await asyncio.to_thread(db.backfill_fts)
    if n:
        print(f"startup: fts backfill — {n} ходов доиндексировано")
    nt = await asyncio.to_thread(sessions.backfill_titles)
    if nt:
        print(f"startup: titles backfill — {nt} сессий названы эвристикой")
    r = await asyncio.to_thread(_revive_abandoned)
    if r:
        print(f"startup: revive — {r} сессий в очередь")
    _scheduler_task = asyncio.create_task(_reaction_loop())
    _queue_task = asyncio.create_task(_queue_loop())
    if cfg.devrun.execute:
        # решение владельца 2026-10-31 «всё на Маке»: прогон доработки исполняет
        # сам сервер в worktree своего репозитория (devexec), а не оболочка ноутбука
        requeued = await asyncio.to_thread(devexec.requeue_interrupted)
        if requeued:
            print("startup: возвращены в очередь оборванные прогоны: "
                  + ", ".join(r[:8] for r in requeued))
        _devrun_task = asyncio.create_task(_devrun_loop())
        print("startup: серверный исполнитель прогонов включён (devexec)")
    if cfg.route.enabled:
        _route_analysis_task = asyncio.create_task(_route_analysis_loop())
        _audit_task = asyncio.create_task(_audit_loop())
    yield
    for t in (_scheduler_task, _queue_task, _route_analysis_task, _audit_task,
              _devrun_task):
        if t:
            t.cancel()
    n = await asyncio.to_thread(sessions.abandon_active_turns)
    if n:
        print(f"shutdown: {n} активных turn'ов → abandoned")
    from . import terminals
    await terminals.close_all()  # bash-процессы не переживают сервер (F11 аудита r2)


app = FastAPI(title="Noosphere", version=__version__, lifespan=lifespan)
# клиент — нативное окно pywebview (страница file://, Origin: null) → preflight OPTIONS.
# G1 (аудит r3): вместо allow_origins=["*"] — regex по локальным адресам
# (localhost / LAN 192.168.* / tailnet 100.*) + null-origin (file:// у pywebview).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(null|https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|100\.\d+\.\d+\.\d+)(:\d+)?)$",
    allow_methods=["*"], allow_headers=["*"])


# G1.4: простой per-IP rate-limit (токен-бакет) на /v1/* — компенсация
# отклонённого F1 (бинд 0.0.0.0 в домашней LAN остаётся решением владельца).
# Конфиг: server.rate_limit {enabled, rpm, burst}. TLS — сознательно открытый
# хвост (зафиксирован в PROTOCOL.md v1.4).
class _RateLimitMiddleware:
    def __init__(self, app):
        self.app = app
        self._buckets: dict[str, list[float]] = {}  # ip -> [tokens, ts]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        cfg = load_config().server.rate_limit or {}
        if not path.startswith("/v1/") or not cfg.get("enabled"):
            await self.app(scope, receive, send)
            return
        import time as _time
        rpm = float(cfg.get("rpm", 120))
        burst = float(cfg.get("burst", 40))
        ip = (scope.get("client") or ("?",))[0]
        now = _time.monotonic()
        tokens, ts = self._buckets.get(ip, [burst, now])
        tokens = min(burst, tokens + (now - ts) * rpm / 60.0)
        if tokens < 1.0:
            retry = int((1.0 - tokens) * 60.0 / rpm) + 1
            self._buckets[ip] = [tokens, now]
            body = '{"detail":"rate limit: превышен потолок запросов"}'.encode("utf-8")
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"retry-after", str(retry).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        self._buckets[ip] = [tokens - 1.0, now]
        await self.app(scope, receive, send)


app.add_middleware(_RateLimitMiddleware)

# Открытие клиента на телефоне: 116 КБ index.html отдавались без сжатия и с
# Cache-Control: no-store, то есть заново на каждый заход (через funnel — секунды).
# gzip сжимает страницу ~в 5 раз, ETag+304 убирает повторную передачу, а no-cache
# (не no-store) разрешает браузеру держать копию и перепроверять её одним запросом.
app.add_middleware(GZipMiddleware, minimum_size=1024)


_LAN_HOST_RE = re.compile(
    r"^(localhost|.*\.local|[a-z0-9-]+)$", re.IGNORECASE)  # без точки = хост LAN


def _is_lan_host(host: str) -> bool:
    """LAN/tailnet/loopback — не внешний адрес. Эвристика по литералу хоста
    (DNS не дёргаем): приватные IPv4, tailnet 100.64/10, ::1, *.local, короткие
    имена без точки. Всё остальное считается внешним."""
    h = (host or "").strip().lower().strip("[]")
    if not h:
        return False
    if h in ("localhost", "::1"):
        return True
    parts = h.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a in (10, 127) or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31) \
                or (a == 169 and b == 254) or (a == 100 and 64 <= b <= 127) or a == 0:
            return True
        return False
    return bool(_LAN_HOST_RE.match(h))


def _addr_of(value: str) -> str:
    """Адрес из звена цепочки: без порта, кавычек и пробелов.
    "[::1]:443" → "::1", "1.2.3.4:5678" → "1.2.3.4", '"1.2.3.4"' → "1.2.3.4"."""
    v = (value or "").strip().strip('"').strip()
    if not v:
        return ""
    if v.startswith("["):                      # [2001:db8::1]:443 / [::1]
        end = v.find("]")
        return v[1:end] if end > 0 else v[1:]
    if v.count(":") == 1:                      # host:port, но не IPv6 без скобок
        return v.split(":", 1)[0].strip()
    return v


def _local_chain(request_or_headers, peer_host: str) -> bool:
    """ВСЯ цепочка адресов запроса — LAN/tailnet/loopback? Тогда LAN доверяем.

    Раньше наличие любого forwarded-заголовка само делало клиента внешним. Для
    tailscale funnel/serve это неверно: они ставят X-Forwarded-For всегда, в том
    числе когда и peer, и адрес в заголовке лежат в tailnet 100.64/10 — телефон
    владельца получал 401 на всё, хотя запрос шёл из его же tailnet.
    Поэтому смотрим на адреса, а не на факт заголовка: peer + X-Forwarded-For
    (через запятую) + X-Real-IP. Внешний адрес в любом звене → False (Bearer).
    Принимает Request/WebSocket/SimpleNamespace с .headers или сами заголовки.
    """
    headers = getattr(request_or_headers, "headers", request_or_headers) or {}
    chain = [_addr_of(peer_host)]
    for name in ("x-forwarded-for", "x-real-ip"):
        raw = headers.get(name) if hasattr(headers, "get") else ""
        if raw:
            chain += [_addr_of(p) for p in str(raw).split(",")]
    hops = [h for h in chain if h]
    if not hops:
        return False                            # адресов нет — не доверяем
    return all(_is_lan_host(h) for h in hops)


def _log_401(client_host: str, headers, why: str) -> None:
    """Причина отказа одной строкой: иначе по access-логу не отличить телефон
    из своего tailnet от настоящего внешнего клиента (гадали 01.11)."""
    log.info("401 client=%s xff=%s xfh=%s forwarded=%s xreal=%s why=%s", client_host,
             headers.get("x-forwarded-for", "-"), headers.get("x-forwarded-host", "-"),
             headers.get("forwarded", "-"), headers.get("x-real-ip", "-"), why)


def _mobile_profile(request: Request) -> dict | None:
    """Профиль мобильного доступа по предъявленному токену nsm_… (или None).

    Токен профиля — отдельная учётка: отзыв профиля гасит его сразу, главный
    bearer владельца при этом не трогается. Результат кэшируется на запрос.
    """
    cached = getattr(request.state, "mobile_profile", None)
    if cached is not None:
        return cached
    hdr = request.headers.get("authorization") or ""
    if not hdr.lower().startswith("bearer "):
        return None
    token = hdr.split(" ", 1)[1].strip()
    if not token.startswith(mobile.TOKEN_PREFIX):
        return None
    prof = mobile.token_profile(token)
    if prof is not None:
        request.state.mobile_profile = prof
    return prof


def _auth(request: Request) -> None:
    cfg = load_config()
    client_host = request.client.host if request.client else ""
    # мобильный токен профиля проверяется ДО любых поблажек loopback/LAN:
    # отозванный токен обязан получать 401, даже если запрос пришёл с localhost
    hdr = request.headers.get("authorization") or ""
    bearer = hdr.split(" ", 1)[1].strip() if hdr.lower().startswith("bearer ") else ""
    if bearer.startswith(mobile.TOKEN_PREFIX):
        prof = _mobile_profile(request)
        if prof is None:
            _log_401(client_host, request.headers, "mobile-token")
            raise HTTPException(401, "bad token")
        mobile.touch_profile(int(prof["id"]))
        sid = request.headers.get("x-mobile-session") or ""
        if sid:
            mobile.session_touch(sid, client_host)
        return
    lan_ok = bool(getattr(cfg.server, "lan_no_auth", False)) and _local_chain(
        request, client_host)
    if lan_ok:
        return  # решение владельца: доверенная LAN/tailnet/loopback без Bearer
    if not cfg.server.token:
        if cfg.server.lan_no_auth:
            _log_401(client_host, request.headers, "external-no-token")
            raise HTTPException(401, "bad token")  # внешний клиент или public proxy
        return  # токен не задан — только localhost (см. config.example.yaml)
    if request.headers.get("authorization") != f"Bearer {cfg.server.token}":
        _log_401(client_host, request.headers, "token")
        raise HTTPException(401, "bad token")


def _auth_tools(request: Request) -> None:
    """_auth + PIN-гейт: мобильная сессия без подтверждённого PIN к инструментам
    не допускается (решение владельца: телефон без PIN — только чтение статуса).
    Обычный bearer владельца и локальные клиенты проходят как раньше."""
    _auth(request)
    prof = _mobile_profile(request)
    if prof is None:
        return
    if not prof.get("pin_hash"):
        return
    sid = request.headers.get("x-mobile-session") or ""
    if not mobile.session_allows_tools(sid, int(prof["id"])):
        raise HTTPException(403, "мобильная сессия не разблокирована PIN")


# ── вход «ссылка + логин» (окно v5, п.9): публичная ручка, выдаёт bearer
# профиля за логин/пароль. Анти-брутфорс: 5 провалов с IP → 429 на 10 минут.
_LOGIN_FAILS: dict[str, list[float]] = {}


class LoginIn(BaseModel):
    login: str
    password: str


@app.post("/v1/login")
async def login_basic(request: Request, inp: LoginIn):
    import time as _time
    host = request.client.host if request.client else "?"
    now = _time.time()
    fails = [t for t in _LOGIN_FAILS.get(host, []) if now - t < 600]
    if len(fails) >= 5:
        raise HTTPException(429, "слишком много попыток — подожди 10 минут")
    try:
        res = await asyncio.to_thread(mobile.login_basic, inp.login, inp.password)
    except mobile.MobileError:
        fails.append(now)
        _LOGIN_FAILS[host] = fails
        raise HTTPException(401, "неверный логин или пароль")
    _LOGIN_FAILS.pop(host, None)
    return {"token": res["token"], "profile_id": res["profile_id"]}


class ChatIn(BaseModel):
    session_id: str | None = None
    user: str | None = None     # None → owner_user из конфига
    task_class: str = "chat"
    prompt: str
    system: str | None = None
    tier: str | None = None
    max_tokens: int | None = None
    require_json: bool = False
    min_chars: int = 0
    must: str | None = None
    phase: str | None = None
    steer: bool = False               # v1.1: приоритетная запись очереди
    attachments: list[str] = []       # v1.1: upload_id вложений
    agent: bool | str | None = None # None → конфиг tools.default_agent; "auto" от новых клиентов (действие по умолчанию);
                                    # явный false — ход без инструментов (планировщик, headless)
    max_tool_iters: int | None = None # опц. потолок итераций (дефолт — конфиг tools.max_iters)
    title: str | None = None          # v1.5: заголовок от /v1/route (≤120 зн, sanitize)
    trial: bool = False               # пробная/нагрузочная сессия: вне вердиктов судьи
                                      # и quality-метрик отчёта (BACKLOG-15 п.5)


class ScheduleIn(BaseModel):
    name: str
    prompt: str
    task_class: str = "chat"
    tier: str | None = None
    interval_minutes: int | None = None
    daily_time: str | None = None     # "HH:MM" локальное
    session_id: str | None = None


class RunIn(BaseModel):
    prompt: str
    session_id: str | None = None
    user: str | None = None     # None → owner_user из конфига
    task_class: str = "chat"
    tier: str | None = None
    system: str | None = None
    agent: bool = True                # прогон без инструментов — редкость
    allow_shell: bool = True          # решение владельца 13.10: ранам shell РАЗРЁШЕН (отступление от аудита r2, F4 — нужен доступ к корпусу данных)
    max_tool_iters: int | None = None
    title: str | None = None          # v1.5: заголовок от /v1/route (≤120 зн, sanitize)


class ProcessIn(BaseModel):
    kind: str = "dev_1c"              # пока единственный: спека → код → судья
    task: str
    project: str | None = None
    max_fix_iters: int = 2            # циклов «код → судья» после первого
    deploy: bool = False              # после ACCEPT — стадия deploy_plan


class RuleIn(BaseModel):
    name: str
    text: str
    ord: int = 100


class VaultIn(BaseModel):
    kind: str
    name: str
    host: str = ""
    login: str = ""
    secret: str = ""
    secret_file: str = ""
    notes: str = ""
    tags: str = ""
    company: str = ""


class RouteIn(BaseModel):
    prompt: str
    session_id: str | None = None   # сессию НЕ создаёт — только контекст


class RouteOverrideIn(BaseModel):
    kind: str | None = None         # chat | agent | run
    project_id: int | None = None


class ProjectIn(BaseModel):
    name: str
    keywords: str = ""


@app.get("/", response_class=HTMLResponse)
async def web_client(request: Request):
    """Существующий клиент в браузере телефона; auth зависит от зоны клиента.

    Отдаём с ETag по mtime+size: повторное открытие — 304 без тела (раньше
    no-store заставлял тянуть 116 КБ каждый раз). Кэш при этом перепроверяемый,
    так что деплой подхватывается сразу.
    """
    page = Path(__file__).resolve().parent.parent / "client" / "web" / "index.html"
    st = page.stat()
    etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304,
                        headers={"ETag": etag, "Cache-Control": "no-cache"})
    return HTMLResponse(page.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache", "ETag": etag})


@app.get("/sw.js")
async def service_worker():
    """Service worker для уведомлений на Android (см. client/web/sw.js).
    Отдаётся с корня, иначе его scope не покроет клиент."""
    page = Path(__file__).resolve().parent.parent / "client" / "web" / "sw.js"
    return Response(page.read_text(encoding="utf-8"), media_type="application/javascript",
                    headers={"Cache-Control": "no-cache",
                             "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
async def manifest():
    """Манифест PWA: без него «Добавить на главный экран» даёт закладку, а не
    standalone-окно, и push на Android не подписывается вовсе."""
    page = Path(__file__).resolve().parent.parent / "client" / "web" / "manifest.webmanifest"
    return Response(page.read_text(encoding="utf-8"),
                    media_type="application/manifest+json",
                    headers={"Cache-Control": "no-cache"})


@app.get("/icons/{name}")
async def icon(name: str):
    """Иконки приложения из client/web/icons (только этот каталог, без ../)."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(404, "no icon")
    page = Path(__file__).resolve().parent.parent / "client" / "web" / "icons" / name
    if not page.exists():
        raise HTTPException(404, "no icon")
    media = "image/png" if page.suffix == ".png" else "image/svg+xml"
    return Response(page.read_bytes(), media_type=media,
                    headers={"Cache-Control": "public, max-age=604800"})


class PushSubIn(BaseModel):
    endpoint: str
    keys: dict = {}


@app.get("/v1/push/key", dependencies=[Depends(_auth)])
async def push_key():
    """Публичный VAPID-ключ + сколько подписок уже есть (диагностика в клиенте)."""
    return {"enabled": push.enabled(), "key": push.public_key() if push.enabled() else "",
            "subs": push.count()}


@app.post("/v1/push/subscribe", dependencies=[Depends(_auth)])
async def push_subscribe(inp: PushSubIn, request: Request):
    if not push.enabled():
        return {"ok": False, "err": "pywebpush не установлен на сервере"}
    keys = inp.keys or {}
    p256dh, auth = keys.get("p256dh") or "", keys.get("auth") or ""
    if not (inp.endpoint and p256dh and auth):
        raise HTTPException(400, "нужны endpoint и keys.p256dh/keys.auth")
    push.save_subscription(inp.endpoint, p256dh, auth,
                           request.headers.get("user-agent", ""))
    return {"ok": True, "subs": push.count()}


@app.post("/v1/push/unsubscribe", dependencies=[Depends(_auth)])
async def push_unsubscribe(inp: PushSubIn):
    push.delete_subscription(inp.endpoint)
    return {"ok": True, "subs": push.count()}


@app.post("/v1/push/test", dependencies=[Depends(_auth)])
async def push_test():
    """Проверка канала: пуш уходит всем подпискам прямо сейчас."""
    if not push.enabled():
        return {"ok": False, "err": "pywebpush не установлен на сервере"}
    if not push.count():
        return {"ok": False, "err": "подписок нет — открой клиент и разреши уведомления"}
    push.notify("Ноосфера · проверка", "пуш доходит, канал живой", tag="test")
    return {"ok": True, "subs": push.count()}


@app.get("/health")
@app.get("/health")
async def health(request: Request):
    cfg = load_config()
    client_host = request.client.host if request.client else ""
    # auth_required — ДЛЯ ЭТОГО запроса, а не глобально: иначе телефон через
    # funnel видел lan_no_auth=true, не показывал вход и ловил 401 на всём
    lan_ok = bool(getattr(cfg.server, "lan_no_auth", False)) and _local_chain(
        request, client_host)
    return {"status": "ok", "version": __version__, "time": now_iso(),
            "lan_no_auth": cfg.server.lan_no_auth,
            "auth_required": bool(cfg.server.token) and not lan_ok,
            "features": _features()}


# E1 (аудит 2026-10-29): каталог скилов липкий на сессию — иначе system-префикс
# меняется ход от хода и кэш провайдера сбрасывается. Замер до правки: auto_block()
# даёт 0 символов на не-1С запрос и 20 433 на 1С-тему, поэтому system одного хода
# — 2 006 символов, соседнего — 22 441, а общий префикс двух ходов одной сессии —
# 1 598 символов из 22 441 (7%). Решение монотонное: каталог включается, как только
# сессия коснулась 1С-темы, и дальше не выключается — префикс стабилен, а не-1С
# сессии за каталог по-прежнему не платят.
_SKILLS_STICKY: dict[str, str] = {}
_SKILLS_STICKY_MAX = 400
# признак уже записан в sessions.meta — не пишем в БД каждый ход
_SKILLS_PERSISTED: set[str] = set()


def _session_skills_block(sid: str, prompt: str) -> str:
    prev = _SKILLS_STICKY.get(sid)
    if prev is None:
        # BACKLOG-15 п.7: словарь живёт в памяти процесса, а сервис перезапускается
        # каждым CI-деплоем (6 деплоев за вечер 29.10). Без признака в БД первый
        # ход 1С-сессии после рестарта снова платил за каталог (20 433 символа
        # ≈ 6,8k токенов) и менял system-префикс, сбрасывая кэш провайдера.
        # Признак «каталог включён» лежит в sessions.meta, словарь — кэш поверх него.
        try:
            prev = (skills.auto_block() or "") \
                if sessions.meta_flag(sid, "skills_block") else ""
        except Exception:                                   # noqa: BLE001
            prev = ""
    try:
        new = skills.auto_block(prompt) or ""
    except Exception:                                       # noqa: BLE001
        new = prev
    keep = new if len(new) > len(prev) else prev
    if sid not in _SKILLS_STICKY and len(_SKILLS_STICKY) >= _SKILLS_STICKY_MAX:
        evicted = next(iter(_SKILLS_STICKY))
        _SKILLS_STICKY.pop(evicted, None)
        _SKILLS_PERSISTED.discard(evicted)
    _SKILLS_STICKY[sid] = keep
    if keep and sid not in _SKILLS_PERSISTED:
        if sessions.set_meta(sid, {"skills_block": True}):
            _SKILLS_PERSISTED.add(sid)
    return keep


async def _run_turn(sid: str, inp: dict, kind: str = "inline") -> dict:
    """Ход с траекторией (v22, 30.10.2026, порт из репозитория GitLab Биокарда,
    проект 534): каждый вызов модели и инструмента — шаг в turn_steps
    (server/trajectory.py). Шаги пишутся и при падении хода — одной вставкой
    в finally."""
    rec, token = trajectory.begin(sid)
    res = None
    try:
        res = await _run_turn_inner(sid, inp, kind)
        return res
    finally:
        trajectory.reset(token)
        rec.flush()
        # PWA в фоне/свёрнута: клиентский опрос молчит, будит только пуш от
        # сервера. Ход не ждёт отправку — она в отдельном потоке (push.notify)
        if res is not None and not inp.get("no_push"):
            try:
                push.notify_turn(sid, inp, res, kind)
            except Exception as e:      # пуш не должен ронять ход
                log.info("push после хода не ушёл: %s", e)


async def _run_turn_inner(sid: str, inp: dict, kind: str = "inline") -> dict:
    """Один ход целиком: append_turn → pool.ask → complete_turn → FTS.
    BUSY держит ВЫЗЫВАЮЩИЙ (chat / _queue_loop / _run_due_schedules)."""
    if inp.get("trial"):
        # признак пробной сессии — в sessions.meta: судья и недельный отчёт её
        # исключают (BACKLOG-15 п.5). Пишем до хода, чтобы первый же ход не
        # попал в выборку.
        await asyncio.to_thread(sessions.set_meta, sid, {"trial": True})
    if await asyncio.to_thread(antiloop.treading_water, sid):
        cfg_al = load_config().antiloop
        alerts.send("crit", "treading_water",
                    f"сессия {sid}: treading-water"
                    + (", ход остановлен" if cfg_al.stop_enabled
                       else " — алерт, ход продолжается (стоп-режим выключен 04.11)"))
        if cfg_al.stop_enabled:
            return {"session_id": sid, "ok": False, "err": "treading-water: стоп", "text": ""}

    att_meta: list[dict] = []
    att_images: list[dict] = []
    if inp.get("attachments"):
        try:
            parts = await asyncio.to_thread(uploads.build_attachment_parts,
                                            inp["attachments"])
        except ValueError as e:
            return {"session_id": sid, "ok": False, "err": str(e), "text": ""}
        att_meta = parts["meta"]
        att_images = parts["images"]
    prompt_llm = inp["prompt"]
    if att_meta:
        # v1.6: текстовые вложения инлайнятся целиком, картинки уходят
        # мультимодально (ниже), бинарные — честным описанием
        for tb in parts["text_blocks"]:
            prompt_llm += "\n\n" + tb
        if parts["notes"]:
            prompt_llm += "\n\n[вложения: " + "; ".join(parts["notes"]) + "]"
        if att_images:
            prompt_llm += ("\n\n[приложены изображения: "
                           + ", ".join(im["name"] for im in att_images) + "]")

    snapshot = {"system": inp.get("system"), "task_class": inp.get("task_class", "chat"),
                "prompt": inp["prompt"], "kind": kind}
    if att_meta:
        snapshot["attachments"] = att_meta
    turn_no = await asyncio.to_thread(sessions.append_turn, sid, snapshot)
    trajectory.bind_turn(turn_no)
    # память сессии: новый ход 'active', в историю попадают только done-ходы
    history = await asyncio.to_thread(sessions.get_history, sid)
    # правила рабочего места — префикс к system каждого хода (батч 4)
    rules_text = await asyncio.to_thread(rules.compose)
    system = inp.get("system")
    if rules_text:
        system = f"{rules_text}\n\n{system}" if system else rules_text
    agent_kwargs: dict = {}
    tool_ctx = {"session_id": sid, "turn_no": turn_no, "reveal_secrets": []}
    agent_flag = inp.get("agent")
    if agent_flag == "auto":  # шим 28.10: клиенты main шлют "auto" — здесь решает классификатор
        agent_flag = None
    if agent_flag is None:
        # решение владельца 2026-10-17 («ответы "я не могу"»): инструменты
        # по умолчанию у любого хода; выключается только явным agent=false
        agent_flag = load_config().tools.default_agent
    if agent_flag:
        cfg_tools = load_config().tools
        # состав хода известен до сборки system: фоновому прогону без явного
        # разрешения (аудит r2, F4) и ходу с выключенным конфигом tools.shell текст
        # про терминал не нужен — блок собирается по контексту (резка 02.11)
        allow_shell = (not (kind == "run" and not inp.get("allow_shell"))
                       and bool(cfg_tools.shell))
        tool_ctx["allow_shell"] = allow_shell
        # AGENT_SYSTEM — база агентского хода (слово владельца 2026-11-01 «делай»).
        # Раньше подставлялся ТОЛЬКО при пустом system (`if not system:` ниже), а
        # непустой rules.compose() его вытеснял — чат-ход терял весь агентский блок
        # (замер runtime 01.11: правила 2188 зн, AGENT_SYSTEM доходил лишь до
        # _delegate). Теперь — префикс каждого агентского хода. Только агентского:
        # блок про инструменты, а безинструментным ниже идёт NO_TOOLS_NOTE.
        agent_block = tools.agent_system(allow_shell=allow_shell)
        system = f"{agent_block}\n\n{system}" if system else agent_block
        # Каталог 1С-скилов — только для явной 1С-темы: не-1С ходы не платят
        # токенами за сотню нерелевантных описаний; skill_list/load доступны всегда.
        # E1: блок берётся липким на сессию (см. _session_skills_block) —
        # стабильный system-префикс = попадание в кэш провайдера
        skills_block = await asyncio.to_thread(_session_skills_block, sid, prompt_llm)
        if skills_block:
            system = f"{system}\n\n{skills_block}" if system else skills_block
        # Список MCP-серверов — в промпт: иначе модель узнаёт о них только из vault
        # и может не позвать MCP вовсе (бой 20.10: «посмотри в rlm» → ноль mcp_call).
        mcp_block = tools.mcp_block()
        if mcp_block:
            system = f"{system}\n\n{mcp_block}" if system else mcp_block
        # Память разговоров — в промпт: без блока новая сессия стартует с нуля
        # и «не помнит» прошлых разговоров (жалоба владельца 29.10); хиты FTS по
        # текущему вопросу + свежие заметки, налог порядка 0,5-1K токенов.
        mem_block = await asyncio.to_thread(_memory_block, prompt_llm)
        if mem_block:
            system = f"{system}\n\n{mem_block}" if system else mem_block
        if cfg_tools.enabled:
            specs = tools.openai_specs()
            if not allow_shell:
                # фоновый прогон без присутствия человека — shell выключен (аудит r2, F4)
                specs = [s for s in specs if s["function"]["name"]
                         not in ("shell_exec", "terminal_exec")]
            # префлайт состава хода (порт из канона 29.10.2026): исключённое названо
            # до первого вызова, а не обнаружено отказом посреди задачи
            pf_block = tools.preflight_block(specs, allow_shell=allow_shell)
            if pf_block:
                prompt_llm = f"{prompt_llm}\n\n{pf_block}"
            agent_kwargs = {"tools": specs,
                            "tool_executor": trajectory.wrap_executor(
                                lambda tc: tools.execute_tool_call(tc, ctx=tool_ctx),
                                tool_ctx),
                            # серверный кап: клиент не может поднять потолок выше 2× конфига
                            "max_tool_iters": min(int(inp.get("max_tool_iters") or cfg_tools.max_iters),
                                                  cfg_tools.max_iters * 2)}
            # AGENT_SYSTEM уже в базе system (см. начало agent-блока)
        else:
            snapshot["agent_disabled"] = True
    if not agent_kwargs.get("tools"):
        # ход без инструментов: без этой строки флеш станции пытается «подтвердить
        # командой», шлюз выбрасывает вызов — пустой ответ (порт из канона 29.10.2026)
        system = f"{system}\n\n{pool.NO_TOOLS_NOTE}" if system else pool.NO_TOOLS_NOTE
    dropped_images = False
    # v1.8: ход с картинками — vision-вызов без инструментов читает вложения, его
    # описание уходит в обычный агентский цикл с инструментами (подписочная vision-
    # модель tools вместе с картинками не принимает, а одноshot ask_vision v1.6
    # инструментов не имел вовсе: модель «рисовала» вызов текстом, ход закрывался
    # done без работы — бой 01.11: «Посмотрю код» + {"command": "ls"} текстом).
    res = await pool.ask(inp.get("task_class", "chat"), prompt_llm,
                         system=system, tier=inp.get("tier"),
                         role="" if inp.get("tier") else "chat",
                         max_tokens=inp.get("max_tokens"),
                         check_json=inp.get("require_json", False),
                         min_chars=inp.get("min_chars", 0), must=inp.get("must"),
                         session_id=sid, user=inp.get("user"), history=history,
                         turn_kind=kind, images=att_images or None,
                         **agent_kwargs)
    if res.get("vision_dropped"):
        # vision не настроен → ход идёт текстом с инструментами, отбой не прячем
        dropped_images = True
        alerts.send("warning", "vision_missing",
                    f"сессия {sid}: вложения-картинки отброшены — "
                    "vision не настроен (channels.<name>.vision)")
    usage = res.get("usage") or {}
    spent = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    if dropped_images:
        # честный fallback (v1.6): отказ не прячем ни в ok, ни в ошибке молча
        note = "Изображения не дошли до модели: vision-канал не настроен."
        if res.get("ok"):
            res["text"] = "⚠ " + note + "\n\n" + res.get("text", "")
        else:
            res["err"] = note + " " + (res.get("err") or "")
    # G1.5: значения reveal'нутых за ход секретов вырезаются до записи
    # снапшота (БД/FTS) — дальше по истории растекается только "***".
    mask = tool_ctx.get("reveal_secrets") or []
    final = {**snapshot, "model": res.get("model"), "ok": res.get("ok"),
             "text": res.get("text", ""), "err": res.get("err", ""),
             "reasoning": res.get("reasoning", ""), "tier": res.get("tier", ""),
             # ход мог закончиться по бюджету (время/токены), а не по результату:
             # помечаем честно, чтобы «не закончено» отличалось от «сделано»
             "incomplete": bool(res.get("incomplete")), "stop": res.get("stop", ""),
             # для ленты после перезагрузки: время хода целиком и usage (окно, кэш)
             "ms": res.get("ms"), "usage": res.get("usage") or {}}
    await asyncio.to_thread(sessions.complete_turn, sid, turn_no, spent, final, mask)
    # канал сессии — по факту хода, а не по тому, что записали при создании.
    # router импортируется локально: на уровне модуля main.py его нет.
    try:
        from . import router as _router
        _ch = _router.channel_of_tier(res.get("tier") or "base")[0]
    except Exception as _e:                                 # noqa: BLE001
        _ch = ""
        print(f"noosphere: sessions.channel не определён ({_e!r})", flush=True)
    if _ch:
        await asyncio.to_thread(sessions.set_channel, sid, _ch)
    # prompt индексируем даже при провале: искать «что я спрашивал» нужно всегда
    text_idx = final["text"]
    for s in mask:
        if s:
            text_idx = text_idx.replace(s, "***")
    turn_id = await asyncio.to_thread(sessions.get_turn_id, sid, turn_no)
    if turn_id:
        await asyncio.to_thread(db.index_turn, sid, turn_id, inp["prompt"], text_idx)
    if turn_no == 1:
        # заголовок новой сессии — фоном, ответ хода не ждёт названия
        asyncio.create_task(_gen_title(sid, inp["prompt"], bool(res.get("ok"))))
    # конспект сессии в память — фоном: следующая сессия вспомнит разговор
    asyncio.create_task(_mem_digest(sid, inp, res))
    return {"session_id": sid, "turn_no": turn_no, **res}


async def _gen_title(sid: str, prompt: str, try_llm: bool) -> None:
    """v1.5: заголовок сессии после первого хода. Ход успешен → LLM (route/base,
    без эскалации, ≤30 tok); ход упал или LLM недоступна → эвристика от промпта.
    Fire-and-forget: ошибка уходит только в stderr, ход уже отвечен."""
    try:
        row = await asyncio.to_thread(
            lambda: db.get_db().execute(
                "SELECT title FROM sessions WHERE id=?", (sid,)).fetchone())
        if not row or row["title"]:
            return  # заголовок уже есть (route-title или ручной) — не перекрываем
        title = ""
        if try_llm:
            try:
                # NO_TOOLS_NOTE обязателен: этот вызов идёт МИМО _run_turn, где
                # подсказка «инструментов нет» и ставится. Без неё флеш станции
                # пытался вызвать инструмент, шлюз выбрасывал разметку и приходил
                # пустой ответ — 35 таких провалов за 7 дней (по ~0,4 с и ~100
                # токенов впустую), и каждая оставляла в turns строку ok=0,
                # которая портила метрики качества (доля fail по судье и
                # err_total считали генерацию титула как провал работы).
                # Потолок 30 токенов резал и успешные ответы (finish=length при
                # completion=30), поэтому поднят до 60: заголовок 3-6 слов в него
                # помещается, а обрыв не лечится ретраем вдвое дороже.
                res = await pool.ask(
                    "route",
                    "Назови диалог 3-6 словами по-русски, без кавычек и точки"
                    " в конце: " + prompt[:500],
                    system=pool.NO_TOOLS_NOTE,
                    # Потолок 300, а не 30/60: проба станции 29.10 показала, что
                    # local/deepseek-flash тратит completion-токены на reasoning
                    # даже при enable_thinking:false — на потолке 30 и 60 ответ
                    # приезжал пустым с finish=length («пустой ответ»), на 300
                    # заголовок доезжает. Кап не принуждает модель генерировать
                    # больше: на успешных вызовах она останавливается сама (finish=stop).
                    tier="base", max_tokens=300, force_no_escalate=True,
                    role="title")
                if res.get("ok"):
                    title = res.get("text", "")
            except Exception as e:
                print(f"title: LLM-генерация не удалась ({e!r})", flush=True)
        title = sessions.sanitize_title(title)
        if not title:
            title = sessions.heuristic_title(prompt)
        if title:
            await asyncio.to_thread(sessions.set_title_if_empty, sid, title)
    except Exception as e:
        print(f"title: генерация заголовка для {sid}: {e!r}", flush=True)


def _memory_block(prompt: str) -> str:
    from . import memory
    try:
        return memory.recall_block(prompt)
    except Exception as e:                                    # noqa: BLE001
        print(f"memory: recall_block не дал блок ({e!r})", flush=True)
        return ""


async def _mem_digest(sid: str, inp: dict, res: dict) -> None:
    """Плывущий конспект сессии в memories (одна строка на сессию, обновляется
    каждым содержательным ходом). Fire-and-forget по образцу _gen_title:
    ошибка уходит в stderr, ход уже отвечен. Пробы и обрывки не конспектируем."""
    try:
        if not res.get("ok") or inp.get("trial"):
            return
        prompt = str(inp.get("prompt") or "")
        text = str(res.get("text") or "")
        if len(prompt) < 30 or len(text) < 200:
            return
        if await asyncio.to_thread(sessions.meta_flag, sid, "trial"):
            return
        from . import memory
        row = await asyncio.to_thread(
            lambda: db.get_db().execute(
                "SELECT title FROM sessions WHERE id=?", (sid,)).fetchone())
        title = row["title"] if row and row["title"] else sid[:8]
        prev = await asyncio.to_thread(memory.session_digest, sid)
        r = await pool.ask(
            "route",
            "Обнови конспект рабочей сессии владельца. Дай ОДНУ строку по-русски "
            "до 40 слов: о чём разговор и к чему пришли (решения, выводы, "
            "следующие шаги). Без кавычек и воды.\n"
            + (f"Прежний конспект: {prev[:400]}\n" if prev else "")
            + f"Новый вопрос: {prompt[:600]}\nНовый ответ (начало): {text[:1500]}",
            system=pool.NO_TOOLS_NOTE, tier="base", max_tokens=300,
            force_no_escalate=True, role="summary")
        note = str(r.get("text") or "").strip().replace("\n", " ")
        if not r.get("ok") or len(note) < 20:
            return
        await asyncio.to_thread(memory.upsert_session_digest, sid,
                                f"Сессия «{title}»: {note}")
    except Exception as e:                                    # noqa: BLE001
        print(f"memory: конспект сессии {sid}: {e!r}", flush=True)


# инструменты чата: мобильная сессия без PIN сюда не проходит (_auth_tools)
_DEVRUN_TARGET = re.compile(r"(ноосфер|noosphere)", re.IGNORECASE)
_DEVRUN_VERB = re.compile(
    r"(добавь|почини|доработай|поправь|сделай|поменяй|убери|внедри|переделай|"
    r"запили|реализуй)", re.IGNORECASE)


async def _maybe_devrun(sid: str, prompt: str) -> dict | None:
    """Фраза про доработку самой Ноосферы → devrun (гейт: слово о ноосфере +
    глагол изменения). Ответ — реплика в той же сессии, чтобы лента помнила."""
    if not _DEVRUN_TARGET.search(prompt) or not _DEVRUN_VERB.search(prompt):
        return None
    rid = await asyncio.to_thread(devruns.start, prompt[:2000], "", f"chat:{sid}")
    short = sessions.heuristic_title(prompt)
    text = (f"Взял в доработку саму Ноосферу: прогон {rid} — «{short}». "
            f"Исполнение: worktree → smoke → автодеплой, следы во вкладке "
            f"«Прогоны». О результате доложу здесь.")
    note = {"kind": "system-note", "prompt": prompt, "ok": True, "text": text}
    tn = await asyncio.to_thread(sessions.append_turn, sid, note)
    await asyncio.to_thread(sessions.complete_turn, sid, tn, 0)
    return {"session_id": sid, "ok": True, "text": text, "devrun": rid,
            "trace": [], "escalated": False, "class": "devrun"}


# инструменты чата: мобильная сессия без PIN сюда не проходит (_auth_tools)
@app.post("/v1/chat", dependencies=[Depends(_auth_tools)])
async def chat(inp: ChatIn, _kind: str = "inline"):
    from . import router

    inp.user = inp.user or load_config().owner_user
    sid = inp.session_id
    if not sid:
        start_tier = inp.tier or router.start_tier(inp.task_class)
        channel = router.channel_of_tier(start_tier)[0]
        sid = await asyncio.to_thread(sessions.create_session, inp.user, channel, inp.phase)

    if inp.title:
        # v1.5: заголовок от классификатора — не перекрывает уже стоящий
        await asyncio.to_thread(sessions.set_title_if_empty, sid, inp.title)

    # самодоработка входом-фразой (окно v5, п.10): «добавь/почини в ноосфере …»
    # → прогон доработки (worktree + smoke + автодеплой), а не обычный ход
    dv = await _maybe_devrun(sid, inp.prompt)
    if dv is not None:
        return dv

    if sid in BUSY:
        kind = "steer" if inp.steer else "queue"
        qid = await asyncio.to_thread(queue.enqueue, sid, inp.model_dump(), kind)
        pos = await asyncio.to_thread(queue.position, sid, qid)
        return {"queued": True, "position": pos, "session_id": sid}

    BUSY.add(sid)  # между проверкой и добавлением нет await — гонки в одном loop нет
    try:
        return await _run_turn(sid, inp.model_dump(), _kind)
    finally:
        BUSY.discard(sid)


@app.websocket("/v1/ws")
async def ws(ws: WebSocket):
    cfg = load_config()
    client_host = ws.client.host if ws.client else ""
    if cfg.server.token and ws.query_params.get("token") != cfg.server.token:
        log.info("/v1/ws reject 4401 client=%s", client_host)
        await ws.close(code=4401)
        return
    await ws.accept()
    t_conn = dt.datetime.now()
    turns = 0
    log.info("/v1/ws connect client=%s", client_host)
    try:
        while True:
            data = await ws.receive_json()
            inp = ChatIn(**data)
            await ws.send_json({"type": "turn_start", "task_class": inp.task_class,
                                "tier": inp.tier})
            res = await _chat_inner(inp)
            turns += 1
            await ws.send_json({"type": "turn_end", **res})
    except WebSocketDisconnect:
        pass
    finally:
        log.info("/v1/ws disconnect client=%s uptime=%.0fs turns=%d", client_host,
                 (dt.datetime.now() - t_conn).total_seconds(), turns)


async def _chat_inner(inp: ChatIn) -> dict:
    # WS-ход — тоже интерактивный ход владельца, но помечаем отдельно (G4:
    # приёмка считает turn_kind inline/ws)
    return await chat(inp, _kind="ws")


@app.get("/v1/queue", dependencies=[Depends(_auth)])
async def list_queue(session_id: str | None = None):
    return await asyncio.to_thread(queue.list_items, session_id)


_RUN_TASKS: dict[str, asyncio.Task] = {}


async def _run_bg(rid: str, sid: str, payload: dict) -> None:
    await asyncio.to_thread(runs.mark, rid, "running")
    try:
        while sid in BUSY:  # сессия занята диалогом — ждём своей очереди
            await asyncio.sleep(1)
        BUSY.add(sid)
        try:
            res = await _run_turn(sid, payload, "run")
        finally:
            BUSY.discard(sid)
        status = "done" if res.get("ok") else "error"
        await asyncio.to_thread(runs.mark, rid, status,
                                res.get("text", ""), res.get("err", ""))
    except asyncio.CancelledError:
        await asyncio.to_thread(runs.mark, rid, "canceled")
        raise
    except Exception as e:
        await asyncio.to_thread(runs.mark, rid, "error", "", repr(e)[:400])
    finally:
        _RUN_TASKS.pop(rid, None)


async def _devrun_bg(rid: str, task: str) -> None:
    """Один прогон доработки на сервере. Задача лежит в _RUN_TASKS, поэтому
    ловец сирот (db.expire_orphan_running) видит прогон живым и не закрывает
    его на середине — раньше он помечал error каждый прогон оболочки ноутбука.
    """
    rep: dict = {"ok": False, "error": ""}
    try:
        rep = await devexec.execute(rid, task)
        if rep.get("ok"):
            log.info("devrun %s: патч применён (commit=%s push=%s restart=%s)",
                     rid[:8], rep.get("committed"), rep.get("pushed"),
                     rep.get("restart"))
        else:
            log.warning("devrun %s: не дошёл до main: %s", rid[:8],
                        rep.get("error") or "?")
            alerts.send("warning", "devrun_failed",
                        f"прогон доработки {rid[:8]} не дошёл до main: "
                        f"{(rep.get('error') or 'пусто')[:300]}")
    except Exception as e:  # noqa: BLE001 — исполнитель не роняет сервер
        log.warning("devrun %s: исполнение упало: %r", rid[:8], e)
        rep = {"ok": False, "error": f"исполнение упало: {e!r}"[:300]}
    finally:
        _RUN_TASKS.pop(rid, None)
    # слово владельца 2026-11-01 («задача в очереди — и тишина»): результат
    # возвращается в чат-источник системной заметкой и пушем на телефон.
    try:
        await _notify_devrun(rid, task, rep)
    except Exception as e:  # noqa: BLE001 — уведомление не роняет сервер
        log.warning("devrun %s: уведомление в чат не ушло: %r", rid[:8], e)
    finally:
        _mark_notified(rid)


def _mark_notified(rid: str) -> None:
    """Маркер «чат уведомлён» для scripts/autorestart.py --wait-file.

    autorestart поднимается из autopatch.apply и до правки 02.11 убивал сервер
    через 1,5 с — раньше, чем _notify_devrun дописывал system-note, поэтому
    финал прогона до чата не доезжал (судья 02.11: «результат в транскрипте не
    подтверждён» в 12 чатах из 15). Теперь рестарт ждёт этот файл (не дольше
    --grace секунд), а маркер ставится при любом исходе уведомления.
    """
    try:
        p = Path(str(load_config().data_dir)) / "devruns" / f"{str(rid)[:12]}.notified"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(now_iso(), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — маркер не важнее рестарта
        log.warning("devrun %s: маркер уведомления не записан: %r", str(rid)[:8], e)


async def _notify_devrun(rid: str, task: str, rep: dict) -> None:
    """Финал прогона — в чат-источник (system-note) и пушем. Окно видит заметку
    через get_history, телефон — через Web Push с переходом в сессию."""
    row = await asyncio.to_thread(devruns.get_devrun, rid) or {}
    src = str((row.get("inp") or {}).get("source") or "")
    short_task = " ".join(str(task or "").split())[:140]
    if rep.get("ok"):
        files = ", ".join(str(f).rsplit("/", 1)[-1] for f in (rep.get("files") or [])[:6])
        done = "+".join(p for p, on in (("commit", rep.get("committed")),
                                        ("push", rep.get("pushed")),
                                        ("рестарт", rep.get("restart"))) if on)
        # факт, а не намерение: статус «дошёл до main» подтверждён проверкой
        # коммита в origin/main (autopatch.verify_in_origin), а не rc=0 пуша
        if rep.get("already"):
            fact = "патч уже был в origin/main"
        elif rep.get("verified") and rep.get("commit_sha"):
            fact = f"коммит {rep['commit_sha']} подтверждён в origin/main"
        elif rep.get("verified"):
            fact = "интеграция подтверждена в origin/main"
        else:
            fact = "интеграция в origin/main НЕ подтверждена — проверь вручную"
        text = (f"✅ Прогон {rid[:8]} дошёл до main: {short_task}\n"
                f"Файлы: {files or '—'}; автопатч: {done or 'применён'}; {fact}.")
        summary = " ".join(str(row.get("result_text") or "").split())
        if summary:
            text += "\nОтчёт исполнителя: " + summary[:500]
    else:
        err = (rep.get("error") or row.get("err") or "неизвестно")
        text = (f"⚠ Прогон {rid[:8]} не дошёл до main: {short_task}\n"
                f"Причина: {str(err)[:300]}")
    sid = src[len("chat:"):] if src.startswith("chat:") else ""
    if sid not in ("", "?"):
        # заметка ДО пуша: push-поток daemon и может не дожить до рестарта
        # (autorestart.kill через 1.5 с); прогон из окна — результат во вкладке «Прогоны»
        note = {"kind": "system-note", "prompt": "", "ok": bool(rep.get("ok")), "text": text}
        tn = await asyncio.to_thread(sessions.append_turn, sid, note)
        await asyncio.to_thread(sessions.complete_turn, sid, tn, 0)
    if not rep.get("skip_push"):
        # recovery уже интегрированных прогонов пишет заметку в чат, но не бомбит телефон
        push.notify("Ноосфера · прогон готов" if rep.get("ok") else "Ноосфера · прогон не удался",
                    text[:180], sid=sid or None, tag="devrun-" + rid[:8])


async def _devrun_loop() -> None:
    """Цикл серверного исполнителя прогонов доработки (devexec).

    Один прогон за раз: правка и smoke не должны идти параллельно. Захват
    атомарный (devruns.pickup) — если прогон уже забрала оболочка ноутбука,
    сервер его не трогает.
    """
    while True:
        try:
            row = await asyncio.to_thread(devexec.next_queued)
            if row:
                rid = row["id"]
                if await asyncio.to_thread(devruns.pickup, rid):
                    _RUN_TASKS[rid] = asyncio.create_task(
                        _devrun_bg(rid, row["task"]))
                    log.info("devrun %s: взят серверным исполнителем (source=%s)",
                             rid[:8], row.get("source") or "-")
                else:
                    await asyncio.to_thread(devexec.release, rid)
        except Exception as e:  # noqa: BLE001
            print(f"devrun loop error: {e!r}")
        await asyncio.sleep(max(2, int(load_config().devrun.poll_seconds)))


class DevRunIn(BaseModel):
    task: str
    base_sha: str = ""
    source: str = ""


class DevRunFinishIn(BaseModel):
    status: str
    summary: str = ""
    patch: str = ""
    patch_hash: str = ""


@app.post("/v1/devruns", dependencies=[Depends(_auth)], status_code=201)
async def devrun_start(inp: DevRunIn):
    """Прогон доработки из окна (goal 2026-10-12): исполняет оболочка ноутбука."""
    from . import devruns
    rid = await asyncio.to_thread(devruns.start, inp.task, inp.base_sha,
                                  inp.source)
    return {"run_id": rid}


@app.get("/v1/devruns", dependencies=[Depends(_auth)])
async def devrun_list(limit: int = 30):
    from . import devruns
    return await asyncio.to_thread(devruns.list_devruns, limit)


@app.get("/v1/devruns/pending-chat", dependencies=[Depends(_auth)])
async def devrun_pending_chat(limit: int = 10):
    """Незабранные прогоны, поставленные из чата: оболочка забирает их сама.

    Объявлен ДО /v1/devruns/{rid}: иначе «pending-chat» уйдёт в rid.
    """
    from . import devruns
    return await asyncio.to_thread(devruns.pending_chat, limit)


@app.post("/v1/devruns/{rid}/pickup", dependencies=[Depends(_auth)])
async def devrun_pickup(rid: str):
    """Атомарный захват queued-прогона оболочкой; повторный вызов — ok=False."""
    from . import devruns
    ok = await asyncio.to_thread(devruns.pickup, rid)
    return {"ok": ok}


@app.get("/v1/devruns/{rid}", dependencies=[Depends(_auth)])
async def devrun_get(rid: str):
    from . import devruns
    row = await asyncio.to_thread(devruns.get_devrun, rid)
    if row is None:
        raise HTTPException(404, "прогон не найден")
    return row


@app.post("/v1/devruns/{rid}/finish", dependencies=[Depends(_auth)])
async def devrun_finish(rid: str, inp: DevRunFinishIn):
    from . import devruns
    await asyncio.to_thread(devruns.finish, rid, inp.status, inp.summary,
                            inp.patch, inp.patch_hash)
    # автопайлот: патч успешного прогона применяется сам — git apply,
    # коммит затронутых файлов, push, перезапуск с selftest+rollback.
    # Ручное подтверждение владельца убрано (goal 2026-10-21).
    if inp.status == "done" and (inp.patch or "").strip():
        from . import autopatch
        # F3 (аудит 2026-10-29): третий аргумент apply() — summary, а не patch_hash,
        # иначе в тему коммита попадал хеш; и ключа "commits" в результате нет —
        # успешный автопатч отдавал клиенту 500 уже ПОСЛЕ push и рестарта.
        res = await asyncio.to_thread(autopatch.apply, rid, inp.patch,
                                      inp.summary or inp.patch_hash)
        return {"ok": True, "applied": res["applied"],
                "committed": res.get("committed", False), "pushed": res["pushed"],
                "restart": res["restart"], "files": res["files"],
                "smoke": res.get("smoke", ""), "error": res["error"]}
    return {"ok": True}


@app.post("/v1/runs", dependencies=[Depends(_auth)], status_code=201)
async def start_run(inp: RunIn):
    from . import router

    inp.user = inp.user or load_config().owner_user
    sid = inp.session_id
    if not sid:
        start_tier = inp.tier or router.start_tier(inp.task_class)
        sid = await asyncio.to_thread(
            sessions.create_session, inp.user, router.channel_of_tier(start_tier)[0], "run")
    if inp.title:
        await asyncio.to_thread(sessions.set_title_if_empty, sid, inp.title)
    payload = inp.model_dump()
    payload["session_id"] = sid
    rid = await asyncio.to_thread(runs.create, sid, payload)
    if inp.session_id:
        # v1.6: origin-сессия должна ПОМНИТЬ про запущенный ран (дефект
        # «что за ран?»): служебный done-ход виден get_history как реплика.
        short = sessions.heuristic_title(inp.prompt)
        note = {"kind": "system-note", "prompt": "", "ok": True,
                "text": f"Запустил фоновый прогон: «{short}» (run {rid}, "
                        f"сессия {sid[:8]}). Результат появится в нём в списке сессий."}
        tn = await asyncio.to_thread(sessions.append_turn, inp.session_id, note)
        await asyncio.to_thread(sessions.complete_turn, inp.session_id, tn, 0)
    _RUN_TASKS[rid] = asyncio.create_task(_run_bg(rid, sid, payload))
    return {"run_id": rid, "session_id": sid}


@app.get("/v1/runs", dependencies=[Depends(_auth)])
async def list_runs(limit: int = 50):
    return await asyncio.to_thread(runs.list_runs, limit)


@app.get("/v1/runs/{rid}", dependencies=[Depends(_auth)])
async def get_run(rid: str):
    row = await asyncio.to_thread(runs.get, rid)
    if row is None:
        raise HTTPException(404, "прогон не найден")
    return row


class RunMarkIn(BaseModel):
    mark: str                       # ok | notit | none
    note: str = ""


@app.post("/v1/runs/{rid}/mark", dependencies=[Depends(_auth)])
async def mark_run(rid: str, inp: RunMarkIn):
    """Метка владельца на прогон (самодоработка, окно v5 п.10): журнал в
    owner_marks по виртуальному sid devrun:<rid> — та же механика приёмки,
    что у чатов, судью не трогает."""
    if inp.mark not in ("ok", "notit", "none"):
        raise HTTPException(400, "mark: ok | notit | none")
    row = await asyncio.to_thread(runs.get, rid)
    if row is None:
        raise HTTPException(404, "прогон не найден")

    def _put():
        db.get_db().execute(
            "INSERT INTO owner_marks (ts, session_id, mark, note) VALUES (?,?,?,?)",
            (now_iso(), f"devrun:{rid}", inp.mark, inp.note[:200]))
        db.get_db().commit()
    await asyncio.to_thread(_put)
    return {"ok": True, "rid": rid, "mark": inp.mark}


@app.post("/v1/runs/{rid}/cancel", dependencies=[Depends(_auth)])
async def cancel_run(rid: str):
    row = await asyncio.to_thread(runs.get, rid)
    if row is None:
        raise HTTPException(404, "прогон не найден")
    if row["status"] in runs.TERMINAL_STATUSES:
        return {"ok": False, "status": row["status"], "note": "прогон уже завершён"}
    task = _RUN_TASKS.get(rid)
    if task:
        task.cancel()
        # asyncio-отмена не убивает дочерний shell терминала — добиваем (F11)
        from . import terminals
        await terminals.kill(row["session_id"] or "default")
    else:  # задача потерялась (рестарт сервера) — фиксируем отмену вручную
        await asyncio.to_thread(runs.mark, rid, "canceled")
    return {"ok": True}


# v1.7: скилы агента (skills.py) и процессы разработки 1С (processes.py).

@app.get("/v1/skills", dependencies=[Depends(_auth)])
async def list_skills():
    return await asyncio.to_thread(skills.catalog)


@app.post("/v1/skills/{name}/toggle", dependencies=[Depends(_auth)])
async def toggle_skill(name: str):
    row = next((s for s in await asyncio.to_thread(skills.catalog)
                if s["name"] == name), None)
    if row is None:
        raise HTTPException(404, "скил не найден")
    ok = await asyncio.to_thread(skills.set_enabled, name, not row["enabled"])
    return {"ok": ok, "enabled": not row["enabled"]}


_PROC_TASKS: dict[str, asyncio.Task] = {}


async def _process_bg(pid: str) -> None:
    try:
        await processes.run_dev_1c(pid)
    except asyncio.CancelledError:
        await asyncio.to_thread(processes.mark, pid, "canceled")
        raise
    except Exception as e:
        await asyncio.to_thread(processes.mark, pid, "error", repr(e)[:400])
    finally:
        _PROC_TASKS.pop(pid, None)


@app.post("/v1/processes", dependencies=[Depends(_auth)], status_code=201)
async def start_process(inp: ProcessIn):
    if inp.kind != "dev_1c":
        raise HTTPException(400, f"неизвестный kind {inp.kind!r}; доступен: dev_1c")
    if not inp.task.strip():
        raise HTTPException(400, "пустая task")
    pid = await asyncio.to_thread(processes.create, inp.kind, inp.task, inp.project,
                                  max(0, min(inp.max_fix_iters, 5)), inp.deploy)
    _PROC_TASKS[pid] = asyncio.create_task(_process_bg(pid))
    return {"process_id": pid}


@app.get("/v1/processes", dependencies=[Depends(_auth)])
async def list_processes_ep(limit: int = 50):
    return await asyncio.to_thread(processes.list_processes, min(limit, 200))


@app.get("/v1/processes/{pid}", dependencies=[Depends(_auth)])
async def get_process(pid: str):
    row = await asyncio.to_thread(processes.get, pid)
    if row is None:
        raise HTTPException(404, "процесс не найден")
    return row


@app.post("/v1/processes/{pid}/cancel", dependencies=[Depends(_auth)])
async def cancel_process(pid: str):
    row = await asyncio.to_thread(processes.get, pid)
    if row is None:
        raise HTTPException(404, "процесс не найден")
    if row["status"] in processes.TERMINAL_STATUSES:
        return {"ok": False, "status": row["status"], "note": "процесс уже завершён"}
    ok = await asyncio.to_thread(processes.cancel, pid)  # мягкая: стоп между стадиями
    task = _PROC_TASKS.get(pid)
    if task and not task.done():
        task.cancel()
    return {"ok": ok}


@app.get("/v1/rules", dependencies=[Depends(_auth)])
async def list_rules():
    return await asyncio.to_thread(rules.list_rules)


@app.post("/v1/rules", dependencies=[Depends(_auth)], status_code=201)
async def add_rule(inp: RuleIn):
    rid = await asyncio.to_thread(rules.add, inp.name, inp.text, inp.ord)
    return {"id": rid}


@app.post("/v1/rules/{rid}/toggle", dependencies=[Depends(_auth)])
async def toggle_rule(rid: int):
    rows = await asyncio.to_thread(rules.list_rules)
    row = next((r for r in rows if r["id"] == rid), None)
    if row is None:
        raise HTTPException(404, "правило не найдено")
    ok = await asyncio.to_thread(rules.set_enabled, rid, not row["enabled"])
    return {"ok": ok, "enabled": not row["enabled"]}


@app.delete("/v1/rules/{rid}", dependencies=[Depends(_auth)])
async def delete_rule(rid: int):
    ok = await asyncio.to_thread(rules.delete, rid)
    if not ok:
        raise HTTPException(404, "правило не найдено")
    return {"ok": True}


@app.get("/v1/memory", dependencies=[Depends(_auth)])
async def list_memory(limit: int = 20):
    from . import memory
    return await asyncio.to_thread(memory.recent, limit)


@app.get("/v1/vault", dependencies=[Depends(_auth)])
async def search_vault(q: str | None = None, limit: int = 50):
    from . import vault
    if q:
        return await asyncio.to_thread(vault.search, q, limit)
    return await asyncio.to_thread(vault.list_recent, limit)


def _audit_vault_rest(summary: str, status: int, note: str) -> None:
    """REST-мутации vault — в tool_audit (F6-хвост аудита r3): kind+name и
    HTTP-статус, БЕЗ значений секретов. Провал журнала не роняет вызов.

    rc унифицирован с остальными инструментами (0=успех, не-0=провал): раньше здесь
    лежал HTTP-статус как есть (200/201), и в любой сводке по rc<>0 успешные vault-
    операции считались «отказом инструмента» (мискач разбора 2026-11-01). HTTP-код
    сохранён в args_summary для отладки."""
    ok = 200 <= status < 300
    summary = f"{summary} [http {status}]"
    try:
        dbh = db.get_db()
        dbh.execute(
            "INSERT INTO tool_audit (ts, session_id, tool, args_summary, out_len, rc, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (now_iso(), None, "vault_rest", summary[:200], 0, 0 if ok else status, note))
        dbh.commit()
    except Exception as e:
        print(f"tool_audit vault_rest write failed: {e!r}")


@app.post("/v1/vault", dependencies=[Depends(_auth)], status_code=201)
async def save_vault(inp: VaultIn):
    from . import vault
    try:
        entry_id = await asyncio.to_thread(
            vault.upsert, inp.kind, inp.name, inp.host, inp.login,
            inp.secret, inp.secret_file, inp.notes, inp.tags, inp.company)
    except ValueError as e:
        await asyncio.to_thread(_audit_vault_rest,
                                f"upsert {inp.kind}/{inp.name}", 400, "refuse")
        raise HTTPException(400, str(e))
    await asyncio.to_thread(_audit_vault_rest, f"upsert {inp.kind}/{inp.name}", 201, "")
    return {"id": entry_id}


class VaultCompanyIn(BaseModel):
    company: str


@app.get("/v1/vault/owner", dependencies=[Depends(_auth)])
async def vault_owner():
    """Доступы владельцу: company/notes/secret_file включены (модели этот слой не виден)."""
    from . import vault
    return await asyncio.to_thread(vault.list_for_owner)


@app.post("/v1/vault/{entry_id}/reveal", dependencies=[Depends(_auth)])
async def vault_reveal(entry_id: int):
    """Разовый reveal владельцу (кнопка «показать/скопировать»); след в tool_audit.

    Секрет берётся из БД или из secret_file (домашний каталог, ≤64 КБ) — vault
    хранит указатели на файлы, поэтому без этого копировать было бы нечего.
    """
    from . import vault
    row = await asyncio.to_thread(vault.reveal_for_owner, entry_id)
    if row is None:
        raise HTTPException(404, "vault not found")
    await asyncio.to_thread(_audit_vault_rest,
                            f"reveal {row.get('kind')}/{row.get('name')}"
                            f" src={row.get('secret_source') or 'none'}", 200, "")
    return row


@app.post("/v1/vault/{entry_id}/company", dependencies=[Depends(_auth)])
async def vault_company(entry_id: int, inp: VaultCompanyIn):
    from . import vault
    ok = await asyncio.to_thread(vault.set_company, entry_id, inp.company)
    await asyncio.to_thread(_audit_vault_rest,
                            f"company id={entry_id} → {inp.company}", 200 if ok else 404, "")
    if not ok:
        raise HTTPException(404, "vault not found")
    return {"ok": True}


@app.delete("/v1/vault/{entry_id}", dependencies=[Depends(_auth)])
async def delete_vault(entry_id: int):
    from . import vault
    ok = await asyncio.to_thread(vault.delete, entry_id)
    await asyncio.to_thread(_audit_vault_rest, f"delete id={entry_id}",
                            200 if ok else 404, "" if ok else "refuse")
    if not ok:
        raise HTTPException(404, "запись не найдена")
    return {"ok": True}


@app.get("/v1/tool_audit", dependencies=[Depends(_auth)])
async def list_tool_audit(limit: int = 50, session_id: str | None = None):
    """Журнал исполнения инструментов (F2+G9): обрезанный аргументный след
    (args_summary) БЕЗ значений секретов, turn_no, rc, метки refuse."""
    sql = ("SELECT id, ts, session_id, turn_no, tool, args_digest, args_summary,"
           " out_len, out_digest, rc, note FROM tool_audit")
    params: list = []
    if session_id:
        sql += " WHERE session_id = ?"
        params.append(session_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = await asyncio.to_thread(
        lambda: db.get_db().execute(sql, params).fetchall())
    return [dict(r) for r in rows]


@app.get("/v1/acceptance", dependencies=[Depends(_auth)])
async def get_acceptance():
    from . import acceptance
    return await asyncio.to_thread(acceptance.status)


@app.post("/v1/acceptance/start", dependencies=[Depends(_auth)])
async def start_acceptance():
    from . import acceptance
    day = await asyncio.to_thread(acceptance.start)
    return {"started": True, "start": day, "goal_days": acceptance.GOAL_DAYS}


def _session_titles(limit: int = 10) -> list[str]:
    """Заголовки последних сессий для контекста классификатора: первый prompt
    сессии (meta.title в протоколе не заведён). Нет заголовка — пропускаем."""
    dbh = db.get_db()
    out: list[str] = []
    for s in dbh.execute("SELECT id FROM sessions ORDER BY updated_at DESC LIMIT ?",
                         (limit,)):
        row = dbh.execute(
            "SELECT snapshot FROM session_turns WHERE session_id = ?"
            " ORDER BY turn_no LIMIT 1", (s["id"],)).fetchone()
        if not row or not row["snapshot"]:
            continue
        try:
            prompt = json.loads(row["snapshot"]).get("prompt") or ""
        except Exception:
            continue
        if prompt.strip():
            out.append(prompt.strip()[:60])
    return out


@app.post("/v1/route", dependencies=[Depends(_auth)])
async def route(inp: RouteIn):
    """Единая точка входа (батч 6): классификация запроса в chat|agent|run +
    проект. Сессию НЕ создаёт; чат/ран стартует клиент по kind отдельным вызовом."""
    if not load_config().route.enabled:
        raise HTTPException(503, "route выключен конфигом (route.enabled)")
    if not inp.prompt.strip():
        raise HTTPException(400, "пустой prompt")
    titles = await asyncio.to_thread(_session_titles)
    res = await route_intent.classify(inp.prompt, titles, inp.session_id)
    prj = res.get("project")
    return {"log_id": res["log_id"], "kind": res["kind"],
            "project": ({"id": prj["id"], "name": prj["name"], "auto": prj["auto"]}
                        if prj else None),
            "title": res["title"], "confidence": res["confidence"],
            "prefilter": res["prefilter"], "threshold": res["threshold"],
            "model": res.get("model", ""), "tier": res.get("tier", ""),
            "ms": res.get("ms", 0), "err": res.get("err", "")}


@app.get("/v1/route_log", dependencies=[Depends(_auth)])
async def list_route_log(limit: int = 50, day: str | None = None):
    """Журнал классификаций (G3): limit + фильтр по локальному дню YYYY-MM-DD.
    Ротация: строки старше 30 дней чистятся тикером (db.rotate_journals)."""
    return await asyncio.to_thread(route_intent.list_log, min(limit, 500), day)


@app.post("/v1/route/{log_id}/override", dependencies=[Depends(_auth)])
async def route_override(log_id: int, inp: RouteOverrideIn):
    """Правка классификации пользователем — самый ценный сигнал. Влияет на
    статистику и ночной анализ; уже отправленное сообщение не переотправляется."""
    try:
        ok = await asyncio.to_thread(
            route_intent.override, log_id, inp.kind, inp.project_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "запись route_log не найдена")
    return {"ok": True}


@app.get("/v1/projects", dependencies=[Depends(_auth)])
async def list_projects():
    """Проекты + число сессий в одноимённой папке."""
    return await asyncio.to_thread(projects.list_projects)


@app.post("/v1/projects", dependencies=[Depends(_auth)], status_code=201)
async def add_project(inp: ProjectIn):
    row = await asyncio.to_thread(projects.add, inp.name, inp.keywords)
    return dict(row)


@app.post("/v1/projects/{project_id}/rename", dependencies=[Depends(_auth)])
async def rename_project(project_id: int, body: dict):
    """Переименование проекта. Папки УЖЕ разложенных сессий не трогаем —
    новое имя получают только новые сессии (осознанный шов, projects.py)."""
    try:
        row = await asyncio.to_thread(
            projects.rename, project_id, str(body.get("name") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if row is None:
        raise HTTPException(404, "проект не найден")
    return row


@app.post("/v1/sessions/{sid}/folder", dependencies=[Depends(_auth)])
async def set_session_folder(sid: str, body: dict):
    ok = await asyncio.to_thread(sessions.set_folder, sid, str(body.get("folder") or ""))
    if not ok:
        raise HTTPException(404, "session not found")
    return {"ok": True}


@app.post("/v1/sessions/{sid}/pin", dependencies=[Depends(_auth)])
async def pin_session(sid: str):
    state = await asyncio.to_thread(sessions.toggle_pin, sid)
    if state is None:
        raise HTTPException(404, "session not found")
    return {"pinned": state}


class RenameIn(BaseModel):
    title: str                     # v1.5: ручное переименование (≤120 зн после sanitize)


@app.post("/v1/sessions/{sid}/rename", dependencies=[Depends(_auth)])
async def rename_session(sid: str, inp: RenameIn):
    if not sessions.sanitize_title(inp.title):
        raise HTTPException(400, "пустой заголовок")
    ok = await asyncio.to_thread(sessions.set_title, sid, inp.title)
    if not ok:
        raise HTTPException(404, "session not found")
    return {"ok": True, "title": sessions.sanitize_title(inp.title)}


@app.post("/v1/stt", dependencies=[Depends(_auth)])
async def stt(request: Request):
    """Голос → текст: прокси к gigastt на CT106 (файл-путь :8899/transcribe).
    Тело — сырые байты аудио (wav/webm), ответ — {text, ms}.
    P2-3 (аудит r3): размер проверяется по Content-Length ДО буферизации;
    сигнатура WAV/RIFF или WebM обязательна; транскрипт с потолком 2000 симв;
    каждая проба пишется в telemetry (task_class='stt')."""
    import time as _time
    import httpx as _httpx

    MAX_BYTES = 25 * 1024 * 1024
    MAX_TEXT = 2000
    cl = request.headers.get("content-length")
    if cl and int(cl) > MAX_BYTES:  # отсекаем ДО чтения тела в память
        raise HTTPException(413, "аудио больше 25 МБ")
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "пустое аудио")
    if len(audio) > MAX_BYTES:
        raise HTTPException(413, "аудио больше 25 МБ")
    if not (audio[:4] == b"RIFF" and audio[8:12] == b"WAVE") \
            and not audio[:4] == b"\x1a\x45\xdf\xa3":
        raise HTTPException(400, "формат не wav/webm (нет сигнатуры RIFF/WAVE или EBML)")
    url = load_config().tools.stt_url
    t0 = _time.time()
    try:
        r = await pool.CLIENT.post(url, content=audio,
                                   headers={"Content-Type": "application/octet-stream"},
                                   timeout=120)
    except _httpx.HTTPError as e:
        await asyncio.to_thread(_stt_telemetry, False, int((_time.time() - t0) * 1000),
                                f"stt недоступен: {e!r}"[:200])
        return {"ok": False, "text": "", "err": f"stt недоступен: {e!r}"[:200]}
    ms = int((_time.time() - t0) * 1000)
    if r.status_code != 200:
        await asyncio.to_thread(_stt_telemetry, False, ms, f"stt http {r.status_code}")
        return {"ok": False, "text": "", "err": f"stt http {r.status_code}", "ms": ms}
    try:
        text = (r.json().get("text") or "").strip()
    except Exception:
        text = ""
    if len(text) > MAX_TEXT:  # потолок транскрипта: дальше он течёт в роутер/чат
        text = text[:MAX_TEXT]
    await asyncio.to_thread(_stt_telemetry, bool(text), ms, "")
    return {"ok": bool(text), "text": text, "ms": ms}


def _stt_telemetry(ok: bool, ms: int, err: str) -> None:
    """Проба STT в telemetry (task_class='stt', канал 'stt') — расход и сбои
    голосового ввода видны в burn (P2-3)."""
    from . import telemetry
    try:
        telemetry.record_turn(telemetry.TurnRec(
            task_class="stt", model="gigastt", channel="stt", ok=ok, answered=True,
            ms=ms, err=err[:400], turn_kind="inline"))
    except Exception as e:
        print(f"stt telemetry failed: {e!r}")


class TtsIn(BaseModel):
    text: str = ""
    voice: str = "arman"
    speed: float = 0.95
    nfe_step: int = 16
    engine: str = "auto"           # auto | espeech | silero


class TtsLatencyIn(BaseModel):
    stop_to_first_ms: int = 0      # клиентский замер «стоп записи → первый звук»
    chars: int = 0
    phrases: int = 0
    engine: str = ""


@app.post("/v1/tts", dependencies=[Depends(_auth)])
async def tts(inp: TtsIn):
    """Текст → речь: клон голоса Армана (ESpeech :8897), фолбэк — Silero :8898.

    Тело {text, voice?, speed?, nfe_step?, engine?}, ответ — audio/wav.
    Пустой текст → 422; длинный режется до voice.MAX_TEXT (600) — в разговоре
    такую фразу всё равно никто не дослушает. Повторная фраза приходит из
    файлового кэша (X-TTS-Cached: 1, <50 мс): «да/сейчас/понял» повторяются
    десятками в день, греть GPU на CT106 незачем. Сбой обоих движков → 503,
    клиент в режиме «Разговор» молча продолжает текстом."""
    text = (inp.text or "").strip()
    if not text:
        raise HTTPException(422, "пустой текст")
    if inp.engine not in voice.ENGINES:
        raise HTTPException(422, f"движок {inp.engine!r}: нужно "
                                f"{'/'.join(voice.ENGINES)}")
    if len(text) > voice.MAX_TEXT:
        text = text[:voice.MAX_TEXT]
    wav, meta = await voice.synth(text, inp.voice, inp.speed, inp.nfe_step, inp.engine)
    await asyncio.to_thread(_tts_telemetry, bool(wav), meta, len(text))
    if not wav:
        raise HTTPException(503, f"tts недоступен: {meta.get('err') or '?'}"[:300])
    return Response(wav, media_type="audio/wav", headers={
        "X-TTS-Engine": str(meta.get("engine") or ""),
        "X-TTS-Cached": "1" if meta.get("cached") else "0",
        "X-TTS-Ms": str(meta.get("ms") or 0),
        "Cache-Control": "no-store",
    })


@app.get("/v1/tts/health", dependencies=[Depends(_auth)])
async def tts_health():
    """Агрегированный статус движков озвучки + размер кэша."""
    return await voice.health()


@app.post("/v1/tts/latency", dependencies=[Depends(_auth)])
async def tts_latency(inp: TtsLatencyIn):
    """Клиентский замер разговора: «стоп записи → первый звук», мс.

    Серверная часть (STT + TTS) видна в своих пробах, но живая задержка цикла
    складывается ещё из LLM-ответа и старта Audio в окне — без этой строки
    непонятно, что именно чинить."""
    ms = max(0, min(int(inp.stop_to_first_ms), 600_000))
    info = {"chars": max(0, min(int(inp.chars), 100_000)),
            "phrases": max(0, min(int(inp.phrases), 10_000)),
            "engine": (inp.engine or "")[:40]}
    await asyncio.to_thread(_tts_latency_telemetry, ms, info)
    log.info("talk: стоп записи → первый звук %d мс (chars=%d, phrases=%d, engine=%s)",
             ms, info["chars"], info["phrases"], info["engine"] or "-")
    return {"ok": True, "ms": ms}


def _tts_telemetry(ok: bool, meta: dict, chars: int) -> None:
    """Проба TTS в telemetry (task_class='tts', канал 'tts'): движок, мс, символы.
    По этим строкам видно, сколько раз разговор ушёл в фолбэк и сколько отдал кэш."""
    from . import telemetry
    try:
        telemetry.record_turn(telemetry.TurnRec(
            task_class="tts", model=str(meta.get("engine") or "tts"), channel="tts",
            ok=ok, answered=True, ms=int(meta.get("ms") or 0),
            err=str(meta.get("err") or "")[:400],
            why=f"chars={chars} cached={int(bool(meta.get('cached')))} "
                f"fallback={int(bool(meta.get('fallback')))}",
            turn_kind="inline"))
    except Exception as e:
        print(f"tts telemetry failed: {e!r}")


def _tts_latency_telemetry(ms: int, info: dict) -> None:
    """Замер клиента в telemetry: «стоп записи → первый звук» (task_class='tts')."""
    from . import telemetry
    try:
        telemetry.record_turn(telemetry.TurnRec(
            task_class="tts", model="client", channel="tts",
            ok=0 < ms < 30_000, answered=True, ms=ms,
            why=f"stop→first_sound chars={info['chars']} phrases={info['phrases']} "
                f"engine={info['engine'] or '-'}",
            turn_kind="inline"))
    except Exception as e:
        print(f"tts latency telemetry failed: {e!r}")


@app.post("/v1/queue/{item_id}/cancel", dependencies=[Depends(_auth)])
async def cancel_queue(item_id: int):
    if not await asyncio.to_thread(queue.cancel, item_id):
        raise HTTPException(409, "запись уже не в очереди")
    return {"ok": True}


@app.get("/v1/search", dependencies=[Depends(_auth)])
async def search(q: str = "", limit: int = 20):
    if not q.strip():
        return []
    return await asyncio.to_thread(db.search_turns, q, limit)


@app.get("/v1/sessions", dependencies=[Depends(_auth)])
async def list_sessions(q: str = "", limit: int | None = None, offset: int = 0,
                        since: str = ""):
    """Список сессий. Без параметров — ровно прежний массив (старый клиент цел).

    С параметрами (v1.8, 01.11.2026, оптимизация списка) — объект:
        sessions — страница выборки (те же поля + turns_count и last_turn_at);
        ids      — все id под текущим фильтром (без страницы) — по нему клиент
                   видит, кто ушёл из выборки;
        total    — сколько всего под фильтром (когда задан limit);
        removed  — id закрытых (архивных) сессий под текущим q: выставляется при
                   since, потому что архивация не двигает updated_at и обычный
                   инкрементальный опрос о ней не узнаёт.

    q — поиск по title и id (регистронезависимый, % и _ — литералы),
    limit/offset — страница, since — только сессии с updated_at > since.
    """
    qs = (q or "").strip()
    since_s = (since or "").strip()
    lim = None if limit is None else max(1, min(int(limit), 1000))
    off = max(0, int(offset or 0))
    if not qs and lim is None and not off and not since_s:
        return await asyncio.to_thread(sessions.list_sessions, set(BUSY))
    rows, meta = await asyncio.to_thread(
        sessions.list_sessions, set(BUSY), lim, off, qs or None, since_s or None, True)
    resp: dict = {"sessions": rows, "ids": meta["ids"]}
    if lim is not None:
        resp["total"] = meta["total"]
    if since_s:
        resp["removed"] = meta["removed"]
    return resp


@app.get("/v1/sessions/{sid}", dependencies=[Depends(_auth)])
async def get_session(sid: str):
    s = await asyncio.to_thread(sessions.get_session, sid)
    if s is None:
        raise HTTPException(404, "session not found")
    return s


@app.post("/v1/sessions/{sid}/resume", dependencies=[Depends(_auth)])
async def resume_session(sid: str):
    return await asyncio.to_thread(sessions.resume, sid)


@app.post("/v1/sessions/{sid}/archive", dependencies=[Depends(_auth)])
async def archive_session(sid: str):
    """Убрать сессию из списка (status='closed'); история по id доступна (29.10.2026)."""
    ok = await asyncio.to_thread(sessions.archive, sid)
    if not ok:
        raise HTTPException(404, "session not found")
    return {"archived": True}


@app.post("/v1/sessions/{sid}/trial", dependencies=[Depends(_auth)])
async def mark_trial(sid: str, trial: bool = True):
    """Пометить сессию пробной или снять метку (BACKLOG-15 п.5).

    Пробная сессия исключается из выборки ночного судьи, из вердиктов и из
    quality-метрик недельного отчёта. Архив этого не даёт: архивированные сессии
    и раньше не брались в судью, но их ВЕРДИКТЫ, вынесенные до архива, продолжали
    считаться в доле fail.
    """
    ok = await asyncio.to_thread(sessions.set_meta, sid, {"trial": bool(trial)})
    if not ok:
        raise HTTPException(404, "session not found")
    return {"session_id": sid, "trial": bool(trial)}


class MarkIn(BaseModel):
    mark: str                        # ok | notit | none (none — снять метку)
    note: str = ""                   # что именно неверно (к «не то»), ≤200 зн


@app.post("/v1/sessions/{sid}/mark", dependencies=[Depends(_auth)])
async def mark_session(sid: str, inp: MarkIn):
    """Приёмка владельцем: золотая метка на чат (ok — принято, notit — не то).

    До 02.11.2026 `owner_marks` не писал никто кроме selftest (0 строк за всё
    время), кнопки в окне не было, и судья ставил partial за неподтверждённую
    приёмку: 21 partial за 30 дней при 29 success. Метка — факт, а не вывод:
    она уходит судье уликой (транскрипт + OWNER_MARK_ADDENDUM) и перевешивает
    его вердикт (`chat_judge._clamp_by_mark`), а при недоступном судье —
    попадает в proxy-вердикт правил (`verdicts.chat_proxy_verdict`).

    Чат сразу пересуживается в фоне: прежний вердикт вынесен без метки и больше
    не действителен. Цена — один вызов судьи на метку (k3.1-pro, ~200 токенов); метки
    редкие, окно kimi (100 вызовов / 5 ч) это не съедает.

    Метка старше последних ходов золото теряет (`stale=true`): она остаётся
    уликой о части чата, а статус считают правила и судья.
    """
    mark = (inp.mark or "").strip().lower()
    if mark not in verdicts.MARKS:
        raise HTTPException(400, f"mark должен быть одним из {'|'.join(verdicts.MARKS)}")

    def _write() -> dict | None:
        row = db.get_db().execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            return None
        verdicts.owner_mark(sid, mark, (inp.note or "").strip()[:200])
        verdicts.recompute_session(sid)      # признаки правил свежи до пересуда
        return verdicts.owner_effect(sid)

    eff = await asyncio.to_thread(_write)
    if eff is None:
        raise HTTPException(404, "session not found")
    asyncio.create_task(_rejudge_after_mark(sid))
    return {"session_id": sid, "mark": mark, "effective": eff.get("mark", ""),
            "stale": bool(eff.get("stale")), "status": eff.get("status", ""),
            "rejudge": "scheduled"}


async def _rejudge_after_mark(sid: str) -> None:
    """Пересуд чата после метки владельца. Не критический контур: ошибка уходит в
    журнал, а не в ответ метки, — ночью чат и так пересудится (`_needs_judge`
    видит метку новее вердикта). mode='mark' отличает эти вердикты от ночных:
    без него расход и доля статусов ночного судьи считались вместе с пересудом."""
    try:
        await chat_judge.judge_chat(sid, mode="mark")
    except Exception as e:                                            # noqa: BLE001
        log.warning("пересуд после метки владельца не удался sid=%s: %r", sid, e)


@app.get("/v1/sessions/{sid}/turns/{turn_no}/trajectory", dependencies=[Depends(_auth)])
async def turn_trajectory(sid: str, turn_no: int):
    """Траектория хода (v22, 30.10.2026): шаги модели и инструментов, тайминги,
    токены — из turn_steps (server/trajectory.py, порт из репозитория GitLab
    Биокарда, проект 534). Нет такого хода — 404. Ход есть, а шагов нет (ход до
    v22 или без вызова модели) — recorded=false, steps=[]."""
    def _load() -> dict | None:
        row = db.get_db().execute(
            "SELECT snapshot, status FROM session_turns WHERE session_id = ? AND turn_no = ?",
            (sid, turn_no)).fetchone()
        if row is None:
            return None
        try:
            snap = json.loads(row["snapshot"] or "{}")
        except Exception:
            snap = {}
        steps = trajectory.steps_for(sid, turn_no)
        return {"session_id": sid, "turn_no": turn_no, "status": row["status"],
                "recorded": bool(steps), "steps": steps,
                "totals": trajectory.totals(steps),
                "turn": {"ms": snap.get("ms"), "model": snap.get("model"),
                         "tier": snap.get("tier"), "ok": snap.get("ok"),
                         "usage": snap.get("usage") or {}},
                **({} if steps else {
                    "note": "ход прошёл до появления траектории (v22, 30.10.2026)"
                            " или без вызова модели и инструмента"})}

    out = await asyncio.to_thread(_load)
    if out is None:
        raise HTTPException(404, "ход не найден")
    return out


@app.get("/v1/schedules", dependencies=[Depends(_auth)])
async def list_schedules():
    return await asyncio.to_thread(scheduler.list_schedules)


@app.post("/v1/schedules", dependencies=[Depends(_auth)])
async def create_schedule(inp: ScheduleIn):
    if not inp.interval_minutes and not inp.daily_time:
        raise HTTPException(400, "нужен interval_minutes или daily_time")
    try:
        sid = await asyncio.to_thread(
            scheduler.create_schedule, inp.name, inp.prompt, inp.task_class, inp.tier,
            inp.interval_minutes, inp.daily_time, inp.session_id)
    except (ValueError, TypeError):
        raise HTTPException(400, "daily_time должен быть формата HH:MM")
    return {"id": sid}


@app.post("/v1/schedules/{schedule_id}/toggle", dependencies=[Depends(_auth)])
async def toggle_schedule(schedule_id: int):
    enabled = await asyncio.to_thread(scheduler.toggle, schedule_id)
    if enabled is None:
        raise HTTPException(404, "schedule not found")
    return {"enabled": enabled}


@app.delete("/v1/schedules/{schedule_id}", dependencies=[Depends(_auth)])
async def delete_schedule(schedule_id: int):
    if not await asyncio.to_thread(scheduler.delete, schedule_id):
        raise HTTPException(404, "schedule not found")
    return {"ok": True}


@app.get("/v1/quota", dependencies=[Depends(_auth)])
async def quota():
    return await asyncio.to_thread(quotas.latest)


@app.post("/v1/quota/probe", dependencies=[Depends(_auth)])
async def quota_probe():
    return await asyncio.to_thread(quotas.probe_all)


@app.get("/v1/burn", dependencies=[Depends(_auth)])
async def burn_table(days: int = 1):
    return await asyncio.to_thread(burn.table, days)


@app.get("/v1/audit", dependencies=[Depends(_auth)])
async def audit_summary():
    """Финальные статусы чатов, контракты оптимизации и сводка (вкладка «Аудит»)."""
    return await asyncio.to_thread(_audit_payload)


def _audit_payload() -> dict:
    c = db.get_db()
    chats = [dict(r) for r in c.execute(
        "SELECT * FROM chat_verdict ORDER BY ts DESC LIMIT 50")]
    if chats:
        # действующая метка владельца к каждому вердикту: по ней видно, что статус
        # перебит приёмкой, а не выведен судьёй (reason это тоже называет)
        marks = {r["session_id"]: r["mark"] for r in c.execute(
            "SELECT session_id, mark FROM owner_marks"
            " WHERE id IN (SELECT MAX(id) FROM owner_marks GROUP BY session_id)")}
        for ch in chats:
            ch["owner_mark"] = marks.get(ch["session_id"], "") or ""
            if ch["owner_mark"] not in ("ok", "notit"):
                ch["owner_mark"] = ""
    contracts = optimizer.pending()
    stats = [dict(r) for r in c.execute(
        "SELECT status, COUNT(*) AS n FROM chat_verdict GROUP BY status")]
    return {"chats": chats, "contracts": contracts, "stats": stats,
            "rules_version": verdicts.RULES_VERSION,
            "judge_prompt_version": chat_judge.JUDGE_PROMPT_VERSION,
            "a1": optimizer.a1_state(),
            "harness": harness_usage.summary(7),
            "latency": verdicts.latency_summary(7),
            "matrix": verdicts.class_model_matrix(7)}


@app.get("/v1/audit/report", dependencies=[Depends(_auth)])
async def audit_report_page(days: int = 7):
    """Недельный HTML-отчёт аудита: самодостаточная страница, 0 LLM-токенов."""
    return HTMLResponse(await asyncio.to_thread(audit_report.build_html,
                                                min(max(days, 1), 90)))


@app.post("/v1/audit/a1/{target}", dependencies=[Depends(_auth)])
async def toggle_a1(target: str):
    """Тумблер авто-применения A1 для обратимой цели (3 ночи одинаковых evidence)."""
    return await asyncio.to_thread(optimizer.toggle_a1, target)


@app.post("/v1/audit/harness_ingest", dependencies=[Depends(_auth)])
async def harness_ingest(request: Request):
    """Read-only ингест usage_record.jsonl харнесов; дедуп через ingest_seen."""
    body = await request.json()
    rows = body if isinstance(body, list) else (body or {}).get("rows", [])
    return await asyncio.to_thread(harness_usage.ingest_rows, rows[:500])


@app.post("/v1/audit/contracts/{cid}/approve", dependencies=[Depends(_auth)])
async def approve_contract(cid: int):
    """Утверждение контракта одной строкой (A2): применение идемпотентно."""
    return await asyncio.to_thread(optimizer.decide, cid, True)


@app.post("/v1/audit/contracts/{cid}/deny", dependencies=[Depends(_auth)])
async def deny_contract(cid: int):
    return await asyncio.to_thread(optimizer.decide, cid, False)


class DeepDiveIn(BaseModel):
    session_ids: list[str] = []
    status: str = ""


@app.post("/v1/audit/deep_dive", dependencies=[Depends(_auth)])
async def deep_dive(inp: DeepDiveIn):
    """LLM-разбор выборки по явному запросу: max-тир, кап 5 чатов на вызов."""
    if not inp.session_ids and inp.status not in ("fail", "partial", "success", "unresolved"):
        raise HTTPException(400, "нужны session_ids или status-фильтр")
    return await chat_judge.deep_dive(inp.session_ids, inp.status)


@app.get("/v1/alerts", dependencies=[Depends(_auth)])
async def list_alerts(status: str | None = None):
    return await asyncio.to_thread(alerts.list_alerts, status)


@app.post("/v1/alerts/{alert_id}/ack", dependencies=[Depends(_auth)])
async def ack_alert(alert_id: int):
    if not await asyncio.to_thread(alerts.ack, alert_id):
        raise HTTPException(404, "alert not found or already acked")
    return {"ok": True}


# роутер вложений — в конце, чтобы auth-зависимость не дублировать в каждом пути
app.include_router(uploads.router, dependencies=[Depends(_auth)])


# ─────────────────────────────────────────────────────────────────────────────
# E2 (аудит 2026-10-29): возможности сервера и события вместо поллинга.
#
# Проблема, которую это закрывает: клиент и сервер личного контура взяты из
# разных линий разработки, поэтому окно опрашивало /v1/asks и /v1/approvals,
# которых здесь нет (404 каждые 4 с, ~16 000 запросов в никуда), а всего на один
# полезный ход приходилось 1 277 GET-опросов. На линии BioHarness тот же поллинг
# исчерпал пул соединений PostgreSQL (5 676 исключений psycopg getconn за два дня).
#
# Решение: состав возможностей определяется по фактическим маршрутам (не хардкодом),
# состояние панелей отдаётся одним запросом-отпечатком (~2 мс), а изменение
# доставляется push-каналом. Один дешёвый запрос в 3 с на соединение вместо
# 6–8 запросов в 4 с на панель.
# ─────────────────────────────────────────────────────────────────────────────

_FEATURES_CACHE: list[str] | None = None


def _features() -> list[str]:
    """Что сервер умеет: имена /v1/*-маршрутов без параметров.

    Клиент прячет панели, которых на сервере нет, вместо вечного 404-поллинга.
    """
    global _FEATURES_CACHE
    if _FEATURES_CACHE is None:
        paths = {getattr(r, "path", "") for r in app.routes}
        _FEATURES_CACHE = sorted(
            p[len("/v1/"):] for p in paths
            if p.startswith("/v1/") and "{" not in p)
    return _FEATURES_CACHE


def _panel_state() -> dict:
    """Отпечатки панелей одним обращением к БД (~2 мс на всю выборку).

    Клиент обновляет панель, только когда её отпечаток изменился, поэтому
    «опрос» здесь — серверная сторона push-канала, а не нагрузка клиента.
    """
    d = db.get_db()

    def q(sql: str):
        try:
            row = d.execute(sql).fetchone()
            return row[0] if row is not None else None
        except Exception:                                   # noqa: BLE001
            return None

    return {
        "sessions": q("SELECT COUNT(*) FROM sessions WHERE status != 'closed'"),
        "turns": q("SELECT MAX(id) FROM turns"),
        "alerts": q("SELECT COUNT(*) || ':' || COALESCE(MAX(id), 0) FROM alerts "
                    "WHERE status IN ('new', 'sent', 'escalated')"),
        "spend": q("SELECT COALESCE(SUM(spent_tokens), 0) || ':' || "
                   "COALESCE(SUM(spent_calls), 0) FROM budgets"),
        "acceptance": q("SELECT COUNT(*) FROM acceptance_events"),
        "devruns": q("SELECT COUNT(*) FROM runs WHERE status = 'queued'"),
        "queue": q("SELECT COUNT(*) FROM queue"),
        # занятость — свойство процесса, не БД: именно её окно опрашивало чаще всего
        "busy": ",".join(sorted(BUSY)),
    }


@app.get("/v1/poll", dependencies=[Depends(_auth)])
async def poll_state():
    """Состояние панелей одним запросом: отпечатки + состав возможностей."""
    v = await asyncio.to_thread(_panel_state)
    return {"features": _features(), "v": v, "ts": now_iso()}


@app.get("/v1/panel", dependencies=[Depends(_auth)])
async def panel_snapshot(sid: str | None = None):
    """Приборная панель окна (макет v5): узлы + расход сегодня + аудит + работа сейчас."""
    return await panel.snapshot(sid)


@app.get("/v1/work", dependencies=[Depends(_auth)])
async def work_now():
    """Живые шаги идущих ходов (in-memory, мгновенно) — полоса «в работе»."""
    return {"items": trajectory.live_snapshot(), "ts": now_iso()}


@app.get("/v1/effectiveness", dependencies=[Depends(_auth)])
async def effectiveness(days: int = 7):
    """Вердикты по контурам (окно v5, п.4): «команда эффективность» в окне."""
    days = max(1, min(days, 30))
    return await asyncio.to_thread(panel.effectiveness, days)


# ── «Нужен ты» (окно v5): единая лента ожиданий владельца ──────────────────
# Источники: сессии с упавшим последним ходом (state=error) и crit-алерты без
# ack за 24ч. Сущности не плодим: сессия/алерт и есть пункт; «не сейчас» —
# waiting_snooze_until в sessions.meta, по алертам — обычный ack.
def _waiting_items() -> list[dict]:
    d = db.get_db()
    day_ago = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(days=1)).isoformat()[:19] + "Z"
    items: list[dict] = []
    for r in d.execute(
            "SELECT id, title, folder, updated_at, meta FROM sessions"
            " WHERE status != 'closed' ORDER BY updated_at DESC LIMIT 100").fetchall():
        try:
            meta = json.loads(r["meta"] or "{}")
        except Exception:                               # noqa: BLE001
            meta = {}
        if (meta.get("waiting_snooze_until") or "") > now_iso():
            continue
        last = d.execute(
            "SELECT ok FROM turns WHERE session_id=? AND turn_last=1"
            " ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        if last and last["ok"] == 0:
            items.append({"kind": "session", "id": r["id"],
                          "title": r["title"] or r["id"][:8],
                          "contour": r["folder"] or "общий",
                          "since": r["updated_at"],
                          "why": "последний ход упал"})
    for r in d.execute(
            "SELECT id, kind, text, ts FROM alerts WHERE severity='crit'"
            " AND status IN ('new','sent','escalated') AND ts>=?"
            " ORDER BY id DESC LIMIT 10", (day_ago,)).fetchall():
        items.append({"kind": "alert", "id": str(r["id"]),
                      "title": (r["text"] or "")[:120], "contour": "контур",
                      "since": r["ts"], "why": "крит-алерт"})
    return items


@app.get("/v1/waiting", dependencies=[Depends(_auth)])
async def waiting_list():
    return {"items": await asyncio.to_thread(_waiting_items), "ts": now_iso()}


class WaitingActIn(BaseModel):
    action: str                     # do | later
    note: str = ""


@app.post("/v1/waiting/{kind}/{item_id}/act", dependencies=[Depends(_auth_tools)])
async def waiting_act(kind: str, item_id: str, inp: WaitingActIn):
    """Действие по пункту «Нужен ты». do: сессия — фоновый ход «продолжи»,
    алерт — ack. later: сессия — отсрочка 4ч (meta), алерт — ack."""
    if inp.action not in ("do", "later"):
        raise HTTPException(400, "action: do | later")
    if kind == "alert":
        ok = await asyncio.to_thread(alerts.ack, int(item_id))
        if not ok:
            raise HTTPException(404, "алерт не найден")
        return {"ok": True, "kind": kind, "id": item_id, "action": inp.action}
    if kind != "session":
        raise HTTPException(400, "kind: session | alert")
    row = await asyncio.to_thread(
        lambda: db.get_db().execute(
            "SELECT id, title FROM sessions WHERE id=? AND status != 'closed'",
            (item_id,)).fetchone())
    if not row:
        raise HTTPException(404, "сессия не найдена")
    if inp.action == "later":
        until = (dt.datetime.now(dt.timezone.utc)
                 + dt.timedelta(hours=4)).isoformat()[:19] + "Z"
        await asyncio.to_thread(sessions.set_meta, item_id,
                                {"waiting_snooze_until": until})
        return {"ok": True, "kind": kind, "id": item_id, "action": "later",
                "until": until}
    prompt = ("Продолжи задачу этой сессии с места обрыва: разбери, почему "
              "прошлый ход упал, доделай и верни результат. Без переспросов — "
              "действуй по контексту.")
    if inp.note:
        prompt += f"\nПометка владельца: {inp.note[:300]}"
    asyncio.get_running_loop().create_task(
        chat(ChatIn(session_id=item_id, prompt=prompt, agent="auto",
                    task_class="agent"), _kind="inline"))
    return {"ok": True, "kind": kind, "id": item_id, "action": "do",
            "started": True}


@app.websocket("/v1/events")
async def events_ws(ws: WebSocket):
    """Push-канал: отпечатки панелей приходят, когда они изменились.

    Авторизация та же, что у HTTP: токен (в query, как у /v1/ws) либо доверенная
    LAN/tailnet при lan_no_auth. Keepalive раз в 30 с — соединение не рвётся.
    """
    cfg = load_config()
    client_host = ws.client.host if ws.client else ""
    tok = ws.query_params.get("token") or ""
    # Семантика ровно как у _auth для HTTP: доверенность считается по ВСЕЙ
    # цепочке адресов (peer + X-Forwarded-For + X-Real-IP), а не по одному
    # ws.client.host. Через tailscale funnel peer/заголовки — tailnet, и HTTP
    # канал такой запрос уже пускает; если бы WS смотрел только на peer, после
    # фикса REST канал событий отваливался бы в 4401.
    lan_ok = bool(getattr(cfg.server, "lan_no_auth", False)) and _local_chain(
        ws, client_host)
    if not lan_ok:                              # внешняя цепочка — как в REST
        if cfg.server.token and tok != cfg.server.token:
            log.info("/v1/events reject 4401 client=%s why=token xff=%s xfh=%s",
                     client_host, ws.headers.get("x-forwarded-for", "-"),
                     ws.headers.get("x-forwarded-host", "-"))
            await ws.close(code=4401)
            return
        if not cfg.server.token and getattr(cfg.server, "lan_no_auth", False):
            log.info("/v1/events reject 4401 client=%s why=no-token-not-lan xff=%s xfh=%s",
                     client_host, ws.headers.get("x-forwarded-for", "-"),
                     ws.headers.get("x-forwarded-host", "-"))
            await ws.close(code=4401)
            return
    await ws.accept()
    t_conn = dt.datetime.now()
    sent = 0
    log.info("/v1/events connect client=%s", client_host)
    last: dict = {}
    idle = 0
    try:
        last = await asyncio.to_thread(_panel_state)
        await ws.send_json({"type": "hello", "features": _features(),
                            "v": last, "ts": now_iso()})
        sent += 1
        while True:
            await asyncio.sleep(3)
            cur = await asyncio.to_thread(_panel_state)
            if cur != last:
                last, idle = cur, 0
                await ws.send_json({"type": "changed", "v": cur, "ts": now_iso()})
                sent += 1
            else:
                idle += 3
                if idle >= 30:
                    idle = 0
                    await ws.send_json({"type": "ping", "ts": now_iso()})
                    sent += 1
    except WebSocketDisconnect:
        pass
    except Exception as e:                                  # noqa: BLE001
        # канал не критичный (клиент переподключится), но молча глотать причину
        # обрыва — значит снова мерить соединения только по lsof
        log.info("/v1/events error client=%s err=%r", client_host, e)
    finally:
        log.info("/v1/events disconnect client=%s uptime=%.0fs sent=%d", client_host,
                 (dt.datetime.now() - t_conn).total_seconds(), sent)
        try:
            await ws.close()
        except Exception:                                   # noqa: BLE001
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Кто вошёл (доводка E2, аудит 2026-10-29).
#
# Клиент личного окна — форк корпоративной линии, поэтому он спрашивает /v1/me,
# /v1/admin/users, /v1/presets и /v1/me/connections. На личной линии их не было:
# Me.load() падал в 404, Me.info оставался null, Me.admin() возвращал false — и
# админ-панели (Телеметрия, Приёмка, Прогоны) не обновлялись НИ по таймеру,
# НИ по push-каналу /v1/events, потому что Poll.start(adm) получает adm=false.
#
# Личный инстанс однопользовательский: тот, кто прошёл _auth (токен или доверенная
# LAN по решению владельца), и есть владелец — owner_user из конфига. Пресетов и
# внешних подключений (Bitrix/GitLab/свои модели) на личной линии нет: доступы
# живут в vault, поэтому честный ответ — пустой список, а не 404.
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/v1/me", dependencies=[Depends(_auth)])
async def whoami():
    owner = load_config().owner_user
    return {"login": owner, "name": owner, "admin": True, "role": "owner",
            "instance": "personal"}


@app.get("/v1/admin/users", dependencies=[Depends(_auth)])
async def admin_users():
    """Инстанс однопользовательский: в списке только владелец."""
    owner = load_config().owner_user
    return [{"login": owner, "name": owner, "admin": True}]


@app.get("/v1/presets", dependencies=[Depends(_auth)])
async def presets_list():
    """Пресетов на личной линии нет — пустой список вместо 404."""
    return []


@app.get("/v1/me/connections", dependencies=[Depends(_auth)])
async def my_connections():
    """Внешние подключения — фича корпоративной линии; здесь доступы в vault."""
    return []


# ═══════════════════ мобильный доступ (образец Hermes Mobile) ════════════════
# Профили подключения телефона: имя, адрес сервера, режим авторизации, срок
# действия, опциональный PIN. Токен профиля — отдельная отзываемая учётка nsm_…;
# она принимается _auth, но к инструментам пускает только сессию с PIN (_auth_tools).
# SSH-туннель и QR: порт всегда из server.port конфига, не хардкод.
# ────────────────────────────────────────────────────────────────────────────

class MobileProfileIn(BaseModel):
    name: str
    server_addr: str
    auth_mode: str = "bearer"           # bearer | basic | mtls
    expires_in_days: int | None = None  # None → mobile.default_expires_days; 0 → бессрочно
    pin: str = ""                       # опциональный PIN (4–12 цифр)
    basic_login: str = ""
    basic_password: str = ""


class MobileProfilePatch(BaseModel):
    name: str | None = None
    server_addr: str | None = None
    auth_mode: str | None = None
    expires_in_days: int | None = None
    pin: str | None = None              # "" → снять PIN
    basic_login: str | None = None
    basic_password: str | None = None


class MobileSessionIn(BaseModel):
    token: str
    pin: str = ""
    client: str = ""


class MobilePinIn(BaseModel):
    pin: str


class MobileFingerprintIn(BaseModel):
    fingerprint: str


def _mobile_http(e: mobile.MobileError) -> HTTPException:
    detail: object = e.detail if not e.extra else {"message": e.detail, **e.extra}
    return HTTPException(e.status, detail)


@app.get("/v1/mobile/status", dependencies=[Depends(_auth)])
async def mobile_status(limit: int = 50):
    """Экран состояния: профили, активные сессии по профилям, последние обращения."""
    return await asyncio.to_thread(mobile.status, limit)


@app.get("/v1/mobile/profiles", dependencies=[Depends(_auth)])
async def mobile_profiles():
    return await asyncio.to_thread(mobile.list_profiles)


@app.post("/v1/mobile/profiles", dependencies=[Depends(_auth)], status_code=201)
async def mobile_profile_create(inp: MobileProfileIn):
    try:
        return await asyncio.to_thread(
            mobile.create, inp.name, inp.server_addr, inp.auth_mode,
            inp.expires_in_days, inp.pin, inp.basic_login, inp.basic_password)
    except mobile.MobileError as e:
        raise _mobile_http(e) from e


@app.patch("/v1/mobile/profiles/{pid}", dependencies=[Depends(_auth)])
async def mobile_profile_update(pid: int, inp: MobileProfilePatch):
    try:
        return await asyncio.to_thread(
            mobile.update, pid, **inp.model_dump(exclude_unset=True))
    except mobile.MobileError as e:
        raise _mobile_http(e) from e


@app.post("/v1/mobile/profiles/{pid}/token", dependencies=[Depends(_auth)], status_code=201)
async def mobile_profile_token(pid: int):
    """Выдать bearer-токен профиля. Показывается ОДИН раз (в БД только sha256)."""
    try:
        return await asyncio.to_thread(mobile.issue_token, pid)
    except mobile.MobileError as e:
        raise _mobile_http(e) from e


@app.post("/v1/mobile/profiles/{pid}/cert", dependencies=[Depends(_auth)], status_code=201)
async def mobile_profile_cert(pid: int, days: int | None = None):
    """Сгенерировать клиентский mTLS-сертификат: отдаётся один раз, хранится отпечаток."""
    try:
        return await asyncio.to_thread(mobile.issue_client_cert, pid, days)
    except mobile.MobileError as e:
        raise _mobile_http(e) from e


@app.post("/v1/mobile/profiles/{pid}/revoke", dependencies=[Depends(_auth)])
async def mobile_profile_revoke(pid: int):
    """Отзыв профиля: токен/сертификат гасятся, живые сессии закрываются."""
    try:
        return await asyncio.to_thread(mobile.revoke, pid)
    except mobile.MobileError as e:
        raise _mobile_http(e) from e


@app.delete("/v1/mobile/profiles/{pid}", dependencies=[Depends(_auth)])
async def mobile_profile_delete(pid: int):
    return await mobile_profile_revoke(pid)


@app.get("/v1/mobile/profiles/{pid}/tunnel", dependencies=[Depends(_auth)])
async def mobile_profile_tunnel(pid: int):
    """Готовая команда SSH-туннеля (порт из конфига) + loopback-адрес и текст QR."""
    try:
        return await asyncio.to_thread(mobile.tunnel, None, pid)
    except mobile.MobileError as e:
        raise _mobile_http(e) from e


@app.get("/v1/mobile/profiles/{pid}/qr.svg", dependencies=[Depends(_auth)])
async def mobile_profile_qr(pid: int, target: str = "command"):
    """QR картинкой: target=command — команда туннеля, target=url — адрес для телефона."""
    try:
        data = await asyncio.to_thread(mobile.tunnel_qr, None, pid)
    except mobile.MobileError as e:
        raise _mobile_http(e) from e
    svg = data["qr_command"] if target != "url" else data["qr_url"]
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@app.post("/v1/mobile/sessions")
async def mobile_session_open(inp: MobileSessionIn, request: Request):
    """Открыть мобильную сессию по токену профиля. PIN проверяет сервер.

    Авторизация здесь своя (токен профиля в теле): эндпоинт должен быть доступен
    ДО того, как сессия открыта — иначе некуда вводить PIN.
    """
    ip = request.client.host if request.client else ""
    client = inp.client or (request.headers.get("user-agent") or "")
    try:
        return await asyncio.to_thread(mobile.open_session, inp.token, inp.pin,
                                       client, ip)
    except mobile.MobileError as e:
        raise _mobile_http(e) from e


@app.post("/v1/mobile/sessions/close-all", dependencies=[Depends(_auth)])
async def mobile_sessions_close_all(profile_id: int | None = None):
    """Кнопка «завершить все»: гасит живые мобильные сессии."""
    n = await asyncio.to_thread(mobile.close_all, profile_id)
    return {"closed": n, "profile_id": profile_id}


@app.post("/v1/mobile/sessions/{sid}/lock")
async def mobile_session_lock(sid: str, request: Request):
    """Заблокировать сессию (PIN снова нужен, инструменты закрыты)."""
    return await asyncio.to_thread(mobile.session_lock, sid)


@app.post("/v1/mobile/sessions/{sid}/unlock")
async def mobile_session_unlock(sid: str, inp: MobilePinIn, request: Request):
    try:
        return await asyncio.to_thread(mobile.session_unlock, sid, inp.pin)
    except mobile.MobileError as e:
        raise _mobile_http(e) from e


@app.post("/v1/mobile/sessions/{sid}/close", dependencies=[Depends(_auth)])
async def mobile_session_close(sid: str):
    return {"closed": await asyncio.to_thread(mobile.close_session, sid)}


@app.get("/v1/mobile/tools", dependencies=[Depends(_auth_tools)])
async def mobile_tools(request: Request):
    """Что открыто мобильной сессии. Без подтверждённого PIN — 403 (см. _auth_tools)."""
    sess = (request.headers.get("x-mobile-session") or "").strip()
    return {"session": sess, "pin_ok": True,
            "tools": ["chat", "sessions", "memory_search", "vault_search", "runs"],
            "note": "Мобильная сессия разблокирована PIN — инструменты доступны."}


@app.post("/v1/mobile/mtls/check", dependencies=[Depends(_auth)])
async def mobile_mtls_check(inp: MobileFingerprintIn):
    """Проверка предъявленного клиентского сертификата по отпечатку."""
    prof = await asyncio.to_thread(mobile.verify_fingerprint, inp.fingerprint)
    if prof is None:
        raise HTTPException(404, "сертификат не соответствует ни одному профилю")
    return {"ok": True, "profile_id": prof["id"], "profile": prof.get("name", "")}
