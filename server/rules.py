"""rules.py — правила рабочего места (FOUNDING §16.3 батч 4).

Редактируемые текстовые правила, которые подставляются в system-промпт каждого
хода (до пользовательского system, если он задан). Это то, чем в BioClaude были
«правила»: стиль ответов, границы, контекст владельца — без переписывания кода.

Таблица rules (schema v5). Порядок — по ord, затем id. Пустой список включённых
правил = compose() вернёт "" (system не трогаем).
"""
from __future__ import annotations

from .db import get_db, now_iso


def add(name: str, text: str, ord: int = 100) -> int:
    db = get_db()
    cur = db.execute(
        "INSERT INTO rules (name, text, enabled, ord, created_at) VALUES (?,?,1,?,?)",
        (name, text, ord, now_iso()))
    db.commit()
    return cur.lastrowid


def set_enabled(rid: int, enabled: bool) -> bool:
    db = get_db()
    cur = db.execute("UPDATE rules SET enabled=? WHERE id=?", (int(enabled), rid))
    db.commit()
    return cur.rowcount > 0


def delete(rid: int) -> bool:
    db = get_db()
    cur = db.execute("DELETE FROM rules WHERE id=?", (rid,))
    db.commit()
    return cur.rowcount > 0


def list_rules() -> list[dict]:
    rows = get_db().execute(
        "SELECT id, name, text, enabled, ord, created_at FROM rules ORDER BY ord, id").fetchall()
    return [dict(r) for r in rows]


def compose() -> str:
    """Блок правил для system-промпта; пусто, если включённых нет."""
    rows = get_db().execute(
        "SELECT name, text FROM rules WHERE enabled=1 ORDER BY ord, id").fetchall()
    if not rows:
        return ""
    body = "\n".join(f"{i}. {r['text']}" for i, r in enumerate(rows, 1))
    return f"# Правила рабочего места\n{body}"
