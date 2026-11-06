"""devruns.py — прогоны доработки Ноосферы из окна (goal 2026-10-12).

Сервер хранит метаданные прогона и патч; исполняет прогон оболочка на ноутбуке
(репозиторий живёт там): headless Qwen Code в изолированном worktree. Apply
патча (commit/push/рестарт) выполняет autopatch автоматически при
успешном завершении прогона (status==done с непустым patch).
"""
from __future__ import annotations

import json

from . import runs
from .db import get_db


def start(task: str, base_sha: str = "", source: str = "") -> str:
    return runs.create("devrun", {"kind": "devrun", "task": task,
                                  "base_sha": base_sha, "source": source})


def pending_chat(limit: int = 10) -> list[dict]:
    """Прогоны, поставленные из чата (source=chat:<sid>) и ещё не забранные
    оболочкой (status=queued). Точка входа «открыл чат — попросил доработать»:
    оболочка ноутбука забирает их pickup_once и исполняет у себя."""
    rows = get_db().execute(
        "SELECT id, inp FROM runs WHERE session_id='devrun' AND status='queued'"
        " ORDER BY created_at LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        try:
            inp = json.loads(r["inp"] or "{}")
        except ValueError:
            continue
        src = str(inp.get("source") or "")
        if not src.startswith("chat:"):
            continue
        out.append({"id": r["id"], "task": str(inp.get("task") or ""),
                    "source": src})
    return out


def queued_devruns(limit: int = 5) -> list[dict]:
    """Все незабранные прогоны доработки (любой источник), старейшие первыми.

    В отличие от pending_chat() не фильтрует по source=chat:* — серверный
    исполнитель (devexec) берёт и прогоны, поставленные из окна.
    """
    rows = get_db().execute(
        "SELECT id, inp, created_at FROM runs WHERE session_id='devrun'"
        " AND status='queued' ORDER BY created_at LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        try:
            inp = json.loads(r["inp"] or "{}")
        except ValueError:
            continue
        out.append({"id": r["id"], "task": str(inp.get("task") or ""),
                    "source": str(inp.get("source") or "")})
    return out


def pickup(rid: str) -> bool:
    """Атомарный захват queued-прогона исполнителем; повторный вызов — False."""
    db = get_db()
    cur = db.execute("UPDATE runs SET status='running' WHERE id=? AND status='queued'",
                     (rid,))
    db.commit()
    return cur.rowcount > 0


def finish(rid: str, status: str, summary: str = "", patch: str = "",
           patch_hash: str = "") -> None:
    runs.mark(rid, status, text=summary)
    db = get_db()
    db.execute("UPDATE runs SET patch=?, patch_hash=? WHERE id=?",
               (patch, patch_hash, rid))
    db.commit()


# Отчёт о состоянии прогона: фиксированный набор значений вместо свободного
# текста. До 02.11 состояние жило только в runs.err, и чат-агент рапортовал
# «сделано» по факту постановки в очередь (судья 02.11: 12 чатов из 15 —
# «результат в транскрипте не подтверждён»). Названия — из доменной модели
# dsh-superagent (MIT): progress/waiting/ready/blocked/failed/dropped.
REPORTS = ("progress", "waiting", "ready", "blocked", "failed", "dropped")


def set_report(rid: str, report: str) -> None:
    """Записать структурированный отчёт прогона; значение вне набора — отказ."""
    if report not in REPORTS:
        raise ValueError(f"report={report!r} вне набора {REPORTS}")
    db = get_db()
    db.execute("UPDATE runs SET report=? WHERE id=?", (report, str(rid)[:12]))
    db.commit()


def get_devrun(rid: str) -> dict | None:
    row = get_db().execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["inp"] = json.loads(d.get("inp") or "{}")
    except ValueError:
        d["inp"] = {}
    return d


def list_devruns(limit: int = 30) -> list[dict]:
    rows = get_db().execute(
        "SELECT id, session_id, status, report, created_at, finished_at, err, patch_hash,"
        " substr(result_text,1,200) AS result_preview,"
        " substr(json_extract(inp,'$.task'),1,140) AS task_preview"
        " FROM runs WHERE session_id='devrun' ORDER BY created_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]
