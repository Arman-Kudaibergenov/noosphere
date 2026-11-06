"""queue.py — серверная очередь ходов (PROTOCOL v1.1).

Зачем: пока по сессии идёт ход, новые входы не теряются и не гонятся
параллельно — они встают в очередь (steer раньше обычных внутри сессии),
а фоновой цикл в main разбирает их по мере освобождения сессий.
Полный вход хода хранится в payload (JSON): при исполнении очередной записи
нужны system/max_tokens/attachments, а не только prompt.
Синхронный модуль; FastAPI вызывает из threadpool.
"""
from __future__ import annotations

import json

from .db import get_db, now_iso

ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("done", "failed", "cancelled")


def enqueue(session_id: str, inp: dict, kind: str) -> int:
    """Ставит вход в очередь; возвращает id записи. kind: queue | steer | revive."""
    db = get_db()
    cur = db.execute(
        "INSERT INTO queue (ts, session_id, user, task_class, tier, prompt, kind, status, payload)"
        " VALUES (?,?,?,?,?,?,?,'queued',?)",
        (now_iso(), session_id, inp.get("user"), inp.get("task_class"), inp.get("tier"),
         inp.get("prompt"), kind, json.dumps(inp, ensure_ascii=False)),
    )
    db.commit()
    return int(cur.lastrowid)


def _steer_first_key(r) -> tuple:
    # внутри сессии steer идёт раньше обычных; в равном kind — по времени
    return (0 if r["kind"] == "steer" else 1, r["ts"], r["id"])


def position(session_id: str, item_id: int) -> int:
    """Место записи в очереди сессии с учётом steer-приоритета (1 = следующая)."""
    rows = get_db().execute(
        "SELECT id, kind, ts FROM queue WHERE session_id = ? AND status = 'queued'",
        (session_id,),
    ).fetchall()
    rows = sorted(rows, key=_steer_first_key)
    for i, r in enumerate(rows):
        if r["id"] == item_id:
            return i + 1
    return 0


def heads() -> list[dict]:
    """По одной голове очереди на сессию (steer первым), глобально — по ts.
    Исполнитель пропускает занятые сессии, поэтому возвращаем все головы."""
    rows = get_db().execute(
        "SELECT * FROM queue WHERE status = 'queued' ORDER BY ts"
    ).fetchall()
    by_session: dict[str, list] = {}
    for r in rows:
        by_session.setdefault(r["session_id"], []).append(r)
    out = [sorted(items, key=_steer_first_key)[0] for items in by_session.values()]
    out.sort(key=lambda r: (r["ts"], r["id"]))
    return [dict(r) for r in out]


def get_item(item_id: int) -> dict | None:
    row = get_db().execute("SELECT * FROM queue WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def set_status(item_id: int, status: str, expect: str | None = None) -> bool:
    """Переводит статус; expect — оптимистичная защёлка (гонка с cancel)."""
    db = get_db()
    if expect:
        cur = db.execute("UPDATE queue SET status = ? WHERE id = ? AND status = ?",
                         (status, item_id, expect))
    else:
        cur = db.execute("UPDATE queue SET status = ? WHERE id = ?", (status, item_id))
    db.commit()
    return cur.rowcount > 0


def cancel(item_id: int) -> bool:
    """Отменяет только queued: running отменять нельзя — ход уже пошёл."""
    return set_status(item_id, "cancelled", expect="queued")


def list_items(session_id: str | None = None) -> list[dict]:
    """Активные записи + последние 10 завершённых (для диагностики в UI)."""
    db = get_db()
    cond = "session_id = ? AND " if session_id else ""
    params = (session_id,) if session_id else ()
    active = db.execute(
        f"SELECT * FROM queue WHERE {cond}status IN ('queued','running') ORDER BY id", params
    ).fetchall()
    done = db.execute(
        f"SELECT * FROM queue WHERE {cond}status IN ('done','failed','cancelled')"
        " ORDER BY id DESC LIMIT 10", params
    ).fetchall()
    return [{"id": r["id"], "session_id": r["session_id"], "kind": r["kind"],
             "status": r["status"], "prompt": r["prompt"], "ts": r["ts"]}
            for r in list(active) + list(done)]


def has_active_revive(session_id: str) -> bool:
    """Антидубль автовозобновления: revive ставится один раз на сессию."""
    row = get_db().execute(
        "SELECT 1 FROM queue WHERE session_id = ? AND kind = 'revive'"
        " AND status IN ('queued','running') LIMIT 1", (session_id,),
    ).fetchone()
    return row is not None
