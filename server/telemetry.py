"""telemetry.py — запись и чтение телеметрии ходов.

Контракт:
    record_turn(rec: TurnRec) -> int          # id строки turns
    turns_for_day(day: str | None = None) -> list[sqlite3.Row]
    burn_rows(days: int = 1) -> list[dict]    # агрегат по каналам для burn.py

Каждый вызов LLM через pool ОБЯЗАН писаться сюда — без счётчика краны не
доказуемы (FOUNDING §3). «ok» = прошла проверка результата, «answered» =
транспорт ответил; различать обязательно (урок llm-router).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .db import get_db, now_iso, today_local
from . import trajectory


@dataclass
class TurnRec:
    session_id: str | None = None
    user: str | None = None
    task_class: str = "chat"
    tier_start: str = ""
    tier_used: str = ""
    model: str = ""
    channel: str = ""
    ok: bool = False
    answered: bool = False
    why: str = ""
    finish: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    ms: int = 0
    err: str = ""
    escalated: bool = False
    content_filter: bool = False
    cost_usd: float = 0.0
    turn_kind: str = ""             # inline | ws | run | schedule | queue | revive (G4)
    # v21 (BACKLOG-15 п.3): принадлежность к ходу и его агрегат. turn_uid общий у
    # всех попыток хода, turn_seq — номер попытки, turn_last=1 стоит на итоговой
    # строке, где лежат СУММАРНЫЕ токены и время хода, а не значения последнего
    # вызова. Заполняет pool: _rec() на каждой попытке, _finish_turn() на итоге.
    turn_uid: str = ""
    turn_seq: int = 0
    turn_last: int = 0
    turn_prompt_tokens: int = 0
    turn_completion_tokens: int = 0
    turn_reasoning_tokens: int = 0
    turn_ms: int = 0
    ts: str = field(default_factory=now_iso)
    day: str = field(default_factory=today_local)


def record_turn(rec: TurnRec) -> int:
    db = get_db()
    d = asdict(rec)
    if not rec.turn_uid:
        # Одиночная запись (STT-проба, ингест чужого леджера, vision-ход) — сама
        # себе ход: без этой отметки дневная сумма по turn_last=1 потеряла бы
        # такие строки, и расход перестал бы сходиться с budgets.
        d.update(turn_last=1, turn_prompt_tokens=rec.prompt_tokens,
                 turn_completion_tokens=rec.completion_tokens,
                 turn_reasoning_tokens=rec.reasoning_tokens, turn_ms=rec.ms)
    cur = db.execute(
        """INSERT INTO turns (ts, day, session_id, user, task_class, tier_start, tier_used,
                              model, channel, ok, answered, why, finish, prompt_tokens,
                              completion_tokens, reasoning_tokens, cache_read_tokens,
                              ms, err, escalated, content_filter, cost_usd, turn_kind,
                              turn_uid, turn_seq, turn_last, turn_prompt_tokens,
                              turn_completion_tokens, turn_reasoning_tokens, turn_ms)
           VALUES (:ts, :day, :session_id, :user, :task_class, :tier_start, :tier_used,
                   :model, :channel, :ok, :answered, :why, :finish, :prompt_tokens,
                   :completion_tokens, :reasoning_tokens, :cache_read_tokens,
                   :ms, :err, :escalated, :content_filter, :cost_usd, :turn_kind,
                   :turn_uid, :turn_seq, :turn_last, :turn_prompt_tokens,
                   :turn_completion_tokens, :turn_reasoning_tokens, :turn_ms)""",
        {**d, "ok": int(rec.ok), "answered": int(rec.answered),
         "escalated": int(rec.escalated), "content_filter": int(rec.content_filter)})
    db.commit()
    trajectory.note_model(rec)   # шаг траектории хода, если ход её ведёт (v22, 30.10.2026)
    return cur.lastrowid


def turns_for_day(day: str | None = None):
    db = get_db()
    day = day or today_local()
    return db.execute("SELECT * FROM turns WHERE day = ? ORDER BY id", (day,)).fetchall()


def burn_rows(days: int = 1) -> list[dict]:
    """Агрегат по каналам за последние N локальных дней: вызовы, токены,
    эскалации, провалы, сгоревшее на контент-фильтре. Непокрытые поля = 0,
    НЕ оценка (дисциплина данных)."""
    db = get_db()
    rows = db.execute(
        """SELECT channel,
                  COUNT(*) AS calls,
                  SUM(prompt_tokens) AS prompt_tokens,
                  SUM(completion_tokens) AS completion_tokens,
                  SUM(reasoning_tokens) AS reasoning_tokens,
                  SUM(cache_read_tokens) AS cache_read_tokens,
                  SUM(escalated) AS escalations,
                  SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures,
                  SUM(CASE WHEN content_filter = 1 THEN prompt_tokens + completion_tokens
                           ELSE 0 END) AS filter_burned_tokens,
                  SUM(cost_usd) AS cost_usd,
                  SUM(ms) AS total_ms
           FROM turns
           WHERE day >= date(?, ?)
           GROUP BY channel ORDER BY calls DESC""",
        (today_local(), f"-{days - 1} days")).fetchall()
    return [dict(r) for r in rows]


def burn_rows_by_class(days: int = 1) -> list[dict]:
    """Та же телеметрия, но с разрезом task_class внутри канала (G3): расход
    классификатора (task_class='route') и stt становится виден в burn."""
    db = get_db()
    rows = db.execute(
        """SELECT channel, task_class,
                  COUNT(*) AS calls,
                  SUM(prompt_tokens) AS prompt_tokens,
                  SUM(completion_tokens) AS completion_tokens,
                  SUM(cost_usd) AS cost_usd,
                  SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failures
           FROM turns
           WHERE day >= date(?, ?)
           GROUP BY channel, task_class ORDER BY calls DESC""",
        (today_local(), f"-{days - 1} days")).fetchall()
    return [dict(r) for r in rows]


def tier_calls_today() -> dict:
    """Счётчики вызовов по тирам за сегодня (псевдоканалы tier:* в budgets)."""
    rows = get_db().execute(
        "SELECT channel, spent_calls FROM budgets WHERE day=? AND channel LIKE 'tier:%'",
        (today_local(),)).fetchall()
    return {r["channel"][5:]: r["spent_calls"] for r in rows}
