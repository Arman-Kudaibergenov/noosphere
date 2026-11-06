"""alerts.py — контур реакции: алерт → владелец в TG → SLA → эскалация.

Принцип «реакция первична» (прецедент: алерты kimi-sub-proxy игнорировались
34 суток). Watchdog/нотификатор без LLM — голый код. crit доставляется
немедленно, warning/info копятся в дайджест. Ошибка доставки никогда не
роняет вызывающего — пишется в alert.meta['delivery_error'].

G2 (аудит r3, P1-r3-1): недоставленный crit (status='new' + delivery_error)
повторяется с backoff 1→5→15 мин, кап 6 попыток → crit «транспорт алертов
сломан» АЛЬТЕРНАТИВНЫМ путём (лог-файл data/alerts-fallback.log + stderr —
основной транспорт в этот момент подозрителен). flush_digest ошибку доставки
не глотает: crit + счётчик сбоев в meta.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .config import load_config
from .db import get_db, now_iso

# backoff повторной доставки недоставленного crit (минуты): 1, 5, 15, дальше
# каждые 15 до капа попыток.
_RETRY_BACKOFF_MIN = (1, 5, 15)
_RETRY_CAP = 6
# crit в status='new' БЕЗ delivery_error старше этого возраста считаем случаем
# «первая доставка не состоялась» (краш между INSERT и _deliver): без этой
# границы он невидим для ретрая навсегда — узкий dead-letter, оставшийся от G2.
_NEW_STALE_MIN = 10


def send(severity: str, kind: str, text: str, meta: dict | None = None) -> int:
    """Создаёт алерт (status='new'); crit доставляется сразу. Возвращает id."""
    db = get_db()
    # дедуп 2026-11-04: после перевода antiloop в режим «алерт, не стоп»
    # повторяющийся провал спамил crit каждые пару секунд (326 за полчаса).
    # Одинаковый crit (kind + начало текста) — не чаще раза в 12 часов.
    if severity == "crit":
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=12)).isoformat()[:19] + "Z"
        # ключ без счётчиков вида «сбой №41» — иначе каждый тик уникален
        # и дедуп не ловит шторм дайджеста (инцидент 04.11: 52 crit/час).
        key = re.sub(r"№\d+", "№", (text or "")[:120])
        rows = db.execute(
            "SELECT id, text FROM alerts WHERE kind=? AND severity='crit'"
            " AND ts>=? ORDER BY id DESC LIMIT 50",
            (kind, cutoff)).fetchall()
        for r in rows:
            if re.sub(r"№\d+", "№", (r["text"] or "")[:120]) == key:
                return r["id"]
    cur = db.execute(
        "INSERT INTO alerts (ts, kind, severity, text, status, meta) VALUES (?,?,?,?,?,?)",
        (now_iso(), kind, severity, text, "new", json.dumps(meta or {}, ensure_ascii=False)))
    db.commit()
    alert_id = cur.lastrowid
    if severity == "crit":
        _deliver(alert_id, text)
    return alert_id


def _deliver(alert_id: int, text: str, status: str = "sent") -> bool:
    ok, err = _dispatch(text)
    db = get_db()
    if ok:
        if status == "escalated":
            db.execute("UPDATE alerts SET status='escalated', escalated_at=? WHERE id=?",
                       (now_iso(), alert_id))
        else:
            db.execute("UPDATE alerts SET status='sent', sent_at=? WHERE id=?",
                       (now_iso(), alert_id))
    else:
        row = db.execute("SELECT meta FROM alerts WHERE id=?", (alert_id,)).fetchone()
        meta = json.loads(row["meta"] or "{}") if row else {}
        meta["delivery_error"] = err
        db.execute("UPDATE alerts SET meta=? WHERE id=?",
                   (json.dumps(meta, ensure_ascii=False), alert_id))
    db.commit()
    return ok


def _dispatch(text: str) -> tuple[bool, str]:
    """Транспорт из конфига. (delivered, error) — исключения гасятся здесь."""
    cfg = load_config().alerts
    try:
        if cfg.transport == "ssh-notify":
            p = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 cfg.ssh_target, cfg.notify_command],
                input=text, capture_output=True, text=True, timeout=20)
            if p.returncode == 0:
                return True, ""
            return False, f"ssh rc={p.returncode}: {(p.stderr or '')[:300]}"
        if cfg.transport == "bot":
            token = ""
            if cfg.bot_token_file:
                f = Path(cfg.bot_token_file).expanduser()
                if f.exists():
                    token = f.read_text(encoding="utf-8").strip()
            if not token or not cfg.bot_chat_id:
                return False, "bot transport: нет token или chat_id"
            r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": cfg.bot_chat_id, "text": text,
                                 "parse_mode": "HTML"}, timeout=15)
            if r.status_code == 200 and r.json().get("ok"):
                return True, ""
            return False, f"bot http {r.status_code}: {r.text[:300]}"
        print(text)  # transport 'log' и fallback для неизвестного
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _age_min(ts: str) -> float:
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


def check_sla() -> int:
    """Эскалация неподтверждённых crit + повторная доставка недоставленных (G2).

    Берёт crit в статусах new/sent/escalated:
    - new (доставка упала, есть delivery_error в meta) — ретрай с backoff
      1→5→15 мин, кап _RETRY_CAP попыток → crit «транспорт алертов сломан»
      альтернативным путём (лог-файл + stderr, НЕ тем же транспортом);
    - sent/escalated — эскалация старше sla_minutes (якорь escalated_at/sent_at,
      чтобы не спамить на каждом тике).
    Вызывается периодически из main. Возвращает число действий.
    """
    sla = load_config().alerts.sla_minutes
    db = get_db()
    rows = db.execute(
        "SELECT * FROM alerts WHERE severity='crit'"
        " AND status IN ('new','sent','escalated') AND acked_at IS NULL").fetchall()
    n = 0
    for r in rows:
        meta = json.loads(r["meta"] or "{}")
        if r["status"] == "new":
            if r["kind"] == "alerting-broken":
                continue  # не доламываем транспорт алертами о поломке транспорта
            if not meta.get("delivery_error"):
                if _age_min(r["ts"]) < _NEW_STALE_MIN:
                    continue  # свежий crit, доставка ещё идёт
                meta["delivery_error"] = "первая доставка не состоялась (нет следа)"
            attempts = int(meta.get("delivery_attempts") or 1)
            if attempts >= _RETRY_CAP:
                # 2026-11-04: без этой проверки каждый тик check_sla плодил новый
                # alerting-broken по одному и тому же зависшему crit (~26 тыс/сутки)
                if not meta.get("transport_broken"):
                    _transport_broken(r, meta)
                continue
            wait = _RETRY_BACKOFF_MIN[min(attempts - 1, len(_RETRY_BACKOFF_MIN) - 1)]
            last_try = meta.get("last_delivery_try") or r["ts"]
            if _age_min(last_try) < wait:
                continue
            meta["delivery_attempts"] = attempts + 1
            meta["last_delivery_try"] = now_iso()
            db.execute("UPDATE alerts SET meta=? WHERE id=?",
                       (json.dumps(meta, ensure_ascii=False), r["id"]))
            db.commit()
            if _deliver(r["id"], r["text"]):
                n += 1
            continue
        anchor = r["escalated_at"] or r["sent_at"] or r["ts"]
        if _age_min(anchor) <= sla:
            continue
        ignore = int(_age_min(r["ts"]))
        if _deliver(r["id"], f"🚨 ЭСКАЛАЦИЯ (игнор {ignore} мин)\n{r['text']}",
                    status="escalated"):
            n += 1
    return n


def _transport_broken(row, meta: dict) -> None:
    """Кап попыток исчерпан: основной транспорт подозрителен, поэтому crit о
    поломке уходит АЛЬТЕРНАТИВНЫМ путём — лог-файл рядом с БД + stderr, плюс
    строка alerts (видна в UI) с пометкой, что повторять её не надо."""
    meta["transport_broken"] = True
    db = get_db()
    db.execute("UPDATE alerts SET meta=? WHERE id=?",
               (json.dumps(meta, ensure_ascii=False), row["id"]))
    db.commit()
    text = (f"🚨 ТРАНСПОРТ АЛЕРТОВ СЛОМАН: crit #{row['id']} не доставлен "
            f"за {_RETRY_CAP} попыток. Последняя ошибка: "
            f"{meta.get('delivery_error', '?')[:300]}\nИсходный текст: {row['text']}")
    try:
        log_path = Path(load_config().data_dir) / "alerts-fallback.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {text}\n")
    except Exception as e:
        print(f"[alerts] fallback-лог недоступен: {e!r}", file=sys.stderr)
    print(text, file=sys.stderr)
    send("crit", "alerting-broken", text[:900])  # строка для UI; check_sla её не ретраит


def flush_digest() -> int:
    """warning/info 'new' старше digest_minutes → один дайджест. Возвращает
    число доставленных алертов (0, если ещё рано или все записи UI-only).
    route_analysis и chat_audit остаются доступны в UI, но в TG не дублируются.
    Ошибка доставки НЕ помечает остальные алерты digested: они остаются 'new',
    плюс crit и счётчик сбоев в meta (ключ digest_failures)."""
    digest_min = load_config().alerts.digest_minutes
    db = get_db()
    rows = db.execute(
        "SELECT * FROM alerts WHERE severity IN ('warning','info') AND status='new'"
        " ORDER BY id").fetchall()
    due_all = [r for r in rows if _age_min(r["ts"]) > digest_min]
    if not due_all:
        return 0
    ui_only = {"route_analysis", "chat_audit"}
    silent = [r for r in due_all if r["kind"] in ui_only]
    due = [r for r in due_all if r["kind"] not in ui_only]

    def mark_digested(items) -> None:
        if not items:
            return
        ids = ",".join(str(r["id"]) for r in items)
        db.execute(f"UPDATE alerts SET status='digested', sent_at=? WHERE id IN ({ids})",
                   (now_iso(),))
        db.commit()

    mark_digested(silent)
    if not due:
        return 0
    lines = [f"Дайджест: {len(due)} алертов", ""]
    lines += [f"• [{r['severity']}/{r['kind']}] {r['text']}" for r in due]
    ok, err = _dispatch("\n".join(lines))
    if not ok:
        row = db.execute("SELECT value FROM meta WHERE key='digest_failures'").fetchone()
        fails = (int(row[0]) if row else 0) + 1
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('digest_failures', ?)",
                   (str(fails),))
        db.commit()
        send("crit", "digest",
             f"дайджест из {len(due)} алертов не доставлен (сбой №{fails}): {err[:300]}")
        return 0
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('digest_failures', '0')")
    mark_digested(due)
    return len(due)


def ack(alert_id: int) -> bool:
    """Подтверждение реакции владельца — останавливает SLA-эскалацию."""
    db = get_db()
    cur = db.execute(
        "UPDATE alerts SET status='acked', acked_at=? WHERE id=? AND status != 'acked'",
        (now_iso(), alert_id))
    db.commit()
    return cur.rowcount > 0


def list_alerts(status: str | None = None, limit: int = 50) -> list[dict]:
    """G2/A7: без параметра — активные (new+sent+escalated), чтобы недоставленный
    и эскалированный crit не были невидимы в шторке; status=all — всё подряд;
    конкретный статус — точный фильтр (обратная совместимость)."""
    db = get_db()
    if status == "all":
        rows = db.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?",
                          (limit,)).fetchall()
    elif status:
        rows = db.execute("SELECT * FROM alerts WHERE status=? ORDER BY id DESC LIMIT ?",
                          (status, limit)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM alerts WHERE status IN ('new','sent','escalated')"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["meta"] = json.loads(d.get("meta") or "{}")
        out.append(d)
    return out
