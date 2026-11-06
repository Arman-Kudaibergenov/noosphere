"""acceptance.py — счётчик приёмки «14 дней» (FOUNDING §16.3, F8 аудита r2,
интерпретируемость — G4 аудита r3).

Критерий (после правки F8 — БЕЗ телефона, батч 5 не построен): 14 подряд дней
дневного цикла (диалог с памятью + инструменты + терминалы) в Ноосфере
**без единого обращения к BioClaude :8790**.

День засчитан, если:
- ≥1 ХОД ВЛАДЕЛЬЦА: turn_kind IN ('inline','ws') И user=<owner_user> (логин
  владельца из конфига, ключ owner_user, дефолт arman; G4 —
  интерактивный ход; runs/scheduler/revive/queue/external НЕ засчитываются —
  у старых строк до schema v10 turn_kind NULL, они тоже не засчитываются);
- 0 вызовов по каналам bioclaude:* за тот же день (признак возврата; источник
  — тот же ингест, так что измерение не требует новых приборов).

Старт/сброс — только решением владельца: POST /v1/acceptance/start.
История стартов — таблица acceptance_events (G4: INSERT OR REPLACE в meta
затирал прошлое; старое meta-значение мигрирует первой строкой при чтении).
Подтверждение действия в клиенте — зона ответственности фронта.
"""
from __future__ import annotations

from datetime import date, timedelta

from .db import get_db, now_iso, today_local

GOAL_DAYS = 14


def _owner() -> str:
    from .config import load_config
    return load_config().owner_user


def _migrate_meta_start() -> None:
    """Старое meta-значение acceptance_start → первая строка acceptance_events."""
    db = get_db()
    row = db.execute("SELECT value FROM meta WHERE key='acceptance_start'").fetchone()
    if not row:
        return
    db.execute("DELETE FROM meta WHERE key='acceptance_start'")
    db.execute(
        "INSERT INTO acceptance_events (ts, action, actor, day) VALUES (?,?,?,?)",
        (now_iso(), "start", "migrated-meta", row[0]))
    db.commit()


def _current_start() -> str | None:
    db = get_db()
    _migrate_meta_start()
    row = db.execute(
        "SELECT day FROM acceptance_events WHERE action='start'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    return row["day"] if row else None


def start(actor: str = "") -> str:
    if not actor:
        from .config import load_config
        actor = load_config().owner_user
    day = today_local()
    db = get_db()
    if _current_start():
        action = "reset"  # повторный старт = сброс отсчёта, фиксируем оба события
        db.execute("INSERT INTO acceptance_events (ts, action, actor) VALUES (?,?,?)",
                   (now_iso(), action, actor))
    db.execute("INSERT INTO acceptance_events (ts, action, actor, day) VALUES (?,?,?,?)",
               (now_iso(), "start", actor, day))
    db.commit()
    return day


def status() -> dict:
    start_val = _current_start()
    if not start_val:
        return {"started": False, "goal_days": GOAL_DAYS}
    start_day = date.fromisoformat(start_val)
    today = date.fromisoformat(today_local())
    db = get_db()
    days = []
    streak = 0
    streak_alive = True
    d = start_day
    while d <= today:
        ds = d.isoformat()
        own = db.execute(
            "SELECT COUNT(*) c FROM turns WHERE day=? AND user=?"
            " AND turn_kind IN ('inline','ws')", (ds, _owner())).fetchone()["c"]
        back = db.execute(
            "SELECT COUNT(*) c FROM turns WHERE day=? AND channel LIKE 'bioclaude:%'",
            (ds,)).fetchone()["c"]
        counted = own >= 1 and back == 0
        days.append({"day": ds, "own_turns": own, "bioclaude_calls": back,
                     "counted": counted})
        if counted and streak_alive:
            streak += 1
        else:
            streak_alive = False
        d += timedelta(days=1)
    return {"started": True, "start": start_val, "goal_days": GOAL_DAYS,
            "streak": streak, "done": streak >= GOAL_DAYS, "days": days}
