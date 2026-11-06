"""scheduler.py — headless-прогоны по расписанию (PROTOCOL v1.1).

Модуль только про данные: CRUD, вычисление next_run, выбор due-записей.
Само исполнение (создание сессии, ход, алерт при провале) делает main —
там же живут BUSY и _run_turn, дублировать их сюда нельзя.
Время хранится в UTC ISO; daily_time интерпретируется в локальном поясе
сервера (UTC+tz_offset_hours из конфига), как дневные бакеты burn.
"""
from __future__ import annotations

import datetime as dt

from .config import load_config
from .db import get_db, now_iso


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _local_tz() -> dt.timezone:
    return dt.timezone(dt.timedelta(hours=load_config().server.tz_offset_hours))


def compute_next(interval_minutes: int | None, daily_time: str | None,
                 after: dt.datetime | None = None) -> str:
    """Следующее наступление: interval → now+N минут; daily_time → ближайшие
    локальные HH:MM (сегодня, если ещё не прошли, иначе завтра)."""
    if interval_minutes:
        base = after or dt.datetime.now(dt.timezone.utc)
        return _iso(base + dt.timedelta(minutes=interval_minutes))
    hh, mm = (int(x) for x in str(daily_time).split(":"))
    tz = _local_tz()
    now_l = (after or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    cand = now_l.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= now_l:
        cand += dt.timedelta(days=1)
    return _iso(cand)


def create_schedule(name: str, prompt: str, task_class: str = "chat",
                    tier: str | None = None, interval_minutes: int | None = None,
                    daily_time: str | None = None, session_id: str | None = None) -> int:
    next_run = compute_next(interval_minutes, daily_time)
    db = get_db()
    cur = db.execute(
        "INSERT INTO schedules (name, prompt, task_class, tier, interval_minutes,"
        " daily_time, session_id, enabled, next_run) VALUES (?,?,?,?,?,?,?,1,?)",
        (name, prompt, task_class, tier, interval_minutes, daily_time, session_id, next_run),
    )
    db.commit()
    return int(cur.lastrowid)


def list_schedules() -> list[dict]:
    rows = get_db().execute("SELECT * FROM schedules ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def toggle(schedule_id: int) -> bool | None:
    """Переключает enabled; None — расписание не найдено."""
    db = get_db()
    row = db.execute("SELECT enabled FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        return None
    new = 0 if row["enabled"] else 1
    db.execute("UPDATE schedules SET enabled = ? WHERE id = ?", (new, schedule_id))
    db.commit()
    return bool(new)


def delete(schedule_id: int) -> bool:
    db = get_db()
    cur = db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
    db.commit()
    return cur.rowcount > 0


def due() -> list[dict]:
    """Включённые расписания, у которых наступил next_run."""
    rows = get_db().execute(
        "SELECT * FROM schedules WHERE enabled = 1 AND next_run <= ?", (now_iso(),)
    ).fetchall()
    return [dict(r) for r in rows]


def mark_run(schedule_id: int, ok: bool, interval_minutes: int | None,
             daily_time: str | None) -> None:
    """Фиксирует прогон: last_run=now, next_run — от момента прогона."""
    db = get_db()
    db.execute(
        "UPDATE schedules SET last_run = ?, next_run = ?, last_status = ? WHERE id = ?",
        (now_iso(), compute_next(interval_minutes, daily_time),
         "ok" if ok else "err", schedule_id),
    )
    db.commit()
