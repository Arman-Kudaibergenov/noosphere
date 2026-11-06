"""panel.py — приборная панель окна (макет okno-maket-5): узлы, расход, аудит.

Один endpoint /v1/panel отдаёт снимок целиком, клиент опрашивает раз в ~20 с.
Узлы проверяются TCP-коннектом с таймаутом 2 с параллельно; список — из
config.yaml panel.nodes, без него — DEFAULT_NODES. Расход и аудит — из
существующих таблиц (burn, turns, alerts), новых сущностей не вводится.
Сбой любой части панель не роняет: узел/блок просто приходит пустым.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone

from . import burn, trajectory
from .config import load_config
from .db import get_db, now_iso, today_local

# Список по умолчанию — живой инвентарь контура на 2026-11-04 (plan/plugins.md).
# Переопределяется в config.yaml: panel.nodes = [{name, note, tcp: "host:port"}].
DEFAULT_NODES: list[dict] = [
    {"name": "mac mini", "self": True, "note": "ядро"},
    {"name": "pve", "tcp": "192.168.0.10:8006", "note": "виртуализация"},
    {"name": "hermes", "tcp": "192.168.0.40:22", "note": "боты"},
    {"name": "vps kz", "tcp": "188.244.115.245:22", "note": "egress"},
    {"name": "vps nl", "tcp": "94.156.131.244:22", "note": "резерв"},
    {"name": "станция ds", "tcp": "100.89.171.47:8020", "note": "скрытно"},
]
_TCP_TIMEOUT = 2.0


async def _check_tcp(host: str, port: int) -> tuple[bool, int | None]:
    t0 = time.monotonic()
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port),
                                       timeout=_TCP_TIMEOUT)
        w.close()
        try:
            await w.wait_closed()
        except Exception:                           # noqa: BLE001
            pass
        return True, int((time.monotonic() - t0) * 1000)
    except Exception:                               # noqa: BLE001
        return False, None


async def _node(node: dict) -> dict:
    if node.get("self"):
        return {"name": node["name"], "ok": True, "ms": 0, "note": node.get("note", "")}
    tcp = node.get("tcp") or ""
    if ":" not in tcp:
        return {"name": node["name"], "ok": None, "ms": None,
                "note": node.get("note", "")}
    host, _, port = tcp.rpartition(":")
    ok, ms = await _check_tcp(host, int(port))
    return {"name": node["name"], "ok": ok, "ms": ms, "note": node.get("note", "")}


async def nodes() -> list[dict]:
    cfg = load_config()
    spec = list(cfg.panel_nodes) or DEFAULT_NODES
    return await asyncio.gather(*[_node(n) for n in spec])


def _sync_part(sid: str | None) -> dict:
    """Синхронная часть снимка (sqlite) — зовётся в потоке."""
    d = get_db()
    day = today_local()

    spend_rows = []
    total_calls = total_tokens = 0
    total_cost = 0.0
    try:
        for r in burn.table(1):
            calls = int(r.get("calls") or 0)
            tokens = int(r.get("spent_tokens") or 0)
            cost = float(r.get("cost_usd") or 0.0)
            if calls or tokens:
                spend_rows.append({"channel": r["channel"], "calls": calls,
                                   "tokens": tokens, "cost_usd": round(cost, 4)})
            total_calls += calls
            total_tokens += tokens
            total_cost += cost
    except Exception:                               # noqa: BLE001 — блок пустой, панель жива
        spend_rows = []

    def q(sql: str, params: tuple = ()):
        try:
            row = d.execute(sql, params).fetchone()
            return row[0] if row is not None else None
        except Exception:                           # noqa: BLE001
            return None

    # окно 24ч: боевой журнал несёт ~26k незакрытых крит-алертов прошлых бурь —
    # «всего открытых» бессмысленно, панель показывает свежие
    from datetime import datetime, timedelta, timezone
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()[:19] + "Z"
    audit = {
        "turns_today": q("SELECT COUNT(*) FROM turns WHERE day=? AND turn_last=1",
                         (day,)) or 0,
        "alerts_open": q("SELECT COUNT(*) FROM alerts"
                         " WHERE status IN ('new','sent','escalated') AND ts>=?",
                         (day_ago,)) or 0,
        "alerts_crit": q("SELECT COUNT(*) FROM alerts"
                         " WHERE status IN ('new','sent','escalated')"
                         " AND severity='crit' AND ts>=?", (day_ago,)) or 0,
    }

    session = None
    if sid:
        row = d.execute("SELECT title, folder FROM sessions WHERE id=?",
                        (sid,)).fetchone()
        last = d.execute(
            "SELECT turn_prompt_tokens FROM turns"
            " WHERE session_id=? AND turn_last=1 ORDER BY id DESC LIMIT 1",
            (sid,)).fetchone()
        cfg = load_config()
        cap = int(getattr(cfg.tools, "compact_above_tokens", 0) or 0)
        ctx = int(last[0]) if last and last[0] else 0
        session = {"sid": sid,
                   "title": (row[0] if row else "") or "",
                   "contour": (row[1] if row else "") or "—",
                   "context_tokens": ctx,
                   "context_pct": min(100, round(100 * ctx / cap)) if cap and ctx else 0}

    return {"spend": {"channels": spend_rows, "calls": total_calls,
                      "tokens": total_tokens, "cost_usd": round(total_cost, 4)},
            "audit": audit,
            "session": session,
            "subscriptions": _subscriptions(),
            "work": trajectory.live_snapshot()}


def _subscriptions() -> list[dict]:
    """Прибор «Подписки» (план 04.11, фаза 5): жива / умирает / мёртва + ресет.

    Новых сущностей не вводится: состав каналов и аккаунтов — из config.yaml
    (accounts.py), здоровье — из meta pool:cool:<channel>:<idx> (куда ротация
    сама пишет при 401/429), опорная цифра — вызовы за день из burn.
    Сбой любого канала не роняет панель: его строка просто пропускается.
    """
    from . import accounts as acc

    today_calls: dict[str, int] = {}
    try:
        for r in burn.table(1):
            today_calls[r["channel"]] = int(r.get("calls") or 0)
    except Exception:                                   # noqa: BLE001
        pass
    cfg = load_config()
    now = datetime.now(timezone.utc)
    out = []
    for ch in getattr(cfg, "channels", {}).values():
        try:
            accs = acc.accounts_of(ch)
            cur = acc.current_index(ch)
            cools: dict[int, datetime] = {}
            for i in range(len(accs)):
                ts = acc._meta_get(f"pool:cool:{ch.name}:{i}")
                if not ts:
                    continue
                try:
                    until = datetime.fromisoformat(ts)
                except ValueError:
                    continue
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if until > now:
                    cools[i] = until
            free = [i for i in range(len(accs)) if i not in cools]
            if not accs:
                status, until_s = "—", ""
            elif cur in cools and not free:
                status = "мёртва"
                until_s = min(cools.values()).astimezone().strftime("%d.%m %H:%M")
            elif cur in cools:
                status = "умирает"      # текущий остывает, запасной ещё жив
                until_s = cools[cur].astimezone().strftime("%d.%m %H:%M")
            else:
                status, until_s = "жива", ""
            # активная проба (scripts/subscriptions_probe.py, cron */30) свежее
            # кулдауна: канал без боевого трафика не накапливает cooldown и без
            # пробы выглядел бы «живым» при мёртвой квоте (alibaba, 05.11)
            probe_raw = acc._meta_get(f"pool:probe:{ch.name}")
            if probe_raw:
                try:
                    probe = json.loads(probe_raw)
                except ValueError:
                    probe = None
                if probe and not probe.get("ok"):
                    status = "мёртва"
                    until_s = probe.get("reset") or probe.get("err", "")[:24]
                elif probe and probe.get("ok") and status == "мёртва":
                    status, until_s = "жива", ""
            label = str(accs[cur].get("label", f"#{cur}")) if accs else "—"
            if len(free) > 1:
                label += f" (+{len(free) - 1} запас)"
            out.append({"name": ch.name, "kind": ch.kind, "status": status,
                        "until": until_s, "account": label,
                        "calls_today": today_calls.get(ch.name, 0)})
        except Exception:                               # noqa: BLE001
            continue
    return out


async def snapshot(sid: str | None = None) -> dict:
    ns, sync = await asyncio.gather(nodes(), asyncio.to_thread(_sync_part, sid))
    return {"ts": now_iso(), "nodes": list(ns), **sync}


def effectiveness(days: int = 7) -> dict:
    """«Эффективность» по контурам (окно v5, п.4): вердикт строкой, не простыня.
    Контур = папка сессий (folder; реестр плагинов-контуров — фаза 2).
    Статус контура: «горит» (есть упавшие сессии или доля fail судьи ≥ 30%) /
    «тихо» (нет ходов за 24ч) / «справляется сам»."""
    d = get_db()
    day_from = (date.today() - timedelta(days=days)).isoformat()
    day_ago = (datetime.now(timezone.utc)
               - timedelta(days=1)).isoformat()[:19] + "Z"

    def blank(c: str) -> dict:
        return {"contour": c, "sessions": 0, "turns": 0, "last_ts": "",
                "verdicts": {}, "waiting": 0}

    contours: dict[str, dict] = {}
    for r in d.execute(
            "SELECT COALESCE(NULLIF(s.folder,''),'общий') AS c,"
            " COUNT(DISTINCT s.id) AS sess,"
            " COUNT(t.id) AS turns, MAX(t.ts) AS last_ts"
            " FROM sessions s LEFT JOIN turns t ON t.session_id=s.id"
            " AND t.day>=? AND t.turn_last=1"
            " WHERE s.status != 'closed' AND s.updated_at >= ?"
            " GROUP BY c", (day_from, day_from)).fetchall():
        v = blank(r["c"])
        v.update(sessions=r["sess"], turns=r["turns"] or 0,
                 last_ts=r["last_ts"] or "")
        contours[r["c"]] = v
    for r in d.execute(
            "SELECT COALESCE(NULLIF(s.folder,''),'общий') AS c, cv.status, COUNT(*) AS n"
            " FROM chat_verdict cv JOIN sessions s ON s.id=cv.session_id"
            " WHERE cv.ts >= ? GROUP BY c, cv.status", (day_from,)).fetchall():
        contours.setdefault(r["c"], blank(r["c"]))["verdicts"][r["status"]] = r["n"]
    for r in d.execute(
            "SELECT COALESCE(NULLIF(s.folder,''),'общий') AS c, COUNT(*) AS n"
            " FROM sessions s WHERE s.status != 'closed' AND"
            " (SELECT ok FROM turns t WHERE t.session_id=s.id AND t.turn_last=1"
            "  ORDER BY t.id DESC LIMIT 1) = 0 GROUP BY c").fetchall():
        contours.setdefault(r["c"], blank(r["c"]))["waiting"] = r["n"]

    out = []
    for c in sorted(contours):
        v = contours[c]
        vds = v["verdicts"]
        judged = sum(vds.values())
        fail_share = (vds.get("fail", 0) / judged) if judged else 0.0
        if v["waiting"] > 0 or fail_share >= 0.3:
            state = "горит"
        elif not v["last_ts"] or v["last_ts"] < day_ago:
            state = "тихо"
        else:
            state = "справляется сам"
        v["state"] = state
        v["fail_share"] = round(fail_share, 2)
        out.append(v)
    need = [v["contour"] for v in out if v["state"] == "горит"]
    headline = ("нужен ты: " + ", ".join(need) + " — остальное само"
                if need else "всё само — ничего не ждёт")
    return {"days": days, "contours": out, "headline": headline}
