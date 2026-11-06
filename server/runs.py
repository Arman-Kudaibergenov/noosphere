"""runs.py — фоновые агентские прогоны (FOUNDING §16.3 батч 3).

Прогон = ход (или цепочка ходов агента) вне HTTP-запроса: стартовал и ушёл,
статус смотришь потом. Отличие от планировщика: расписания — периодические,
прогон — разовый и обычно длинный (агент с инструментами).

Таблица runs (schema v4). Статусы: queued → running → done | error | canceled.
"""
from __future__ import annotations

import json
import uuid

from .db import get_db, now_iso

TERMINAL_STATUSES = ("done", "error", "canceled")


def create(session_id: str, inp: dict) -> str:
    rid = uuid.uuid4().hex[:12]
    db = get_db()
    db.execute(
        "INSERT INTO runs (id, session_id, inp, status, created_at) VALUES (?,?,?,?,?)",
        (rid, session_id, json.dumps(inp, ensure_ascii=False), "queued", now_iso()))
    db.commit()
    return rid


def mark(rid: str, status: str, text: str = "", err: str = "") -> None:
    db = get_db()
    finished = now_iso() if status in TERMINAL_STATUSES else None
    db.execute(
        "UPDATE runs SET status=?, result_text=?, err=?,"
        " finished_at=COALESCE(?, finished_at) WHERE id=?",
        (status, text, err, finished, rid))
    db.commit()


def get(rid: str) -> dict | None:
    row = get_db().execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else None


def list_runs(limit: int = 50) -> list[dict]:
    rows = get_db().execute(
        "SELECT id, session_id, status, created_at, finished_at, err,"
        " substr(result_text, 1, 200) AS result_preview"
        " FROM runs ORDER BY created_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]
