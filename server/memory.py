"""memory.py — долговременная память рабочего места (FOUNDING §16.3 батч 4).

Агент сам решает, что запомнить (memory_save), и ищет по памяти полнотекстово
(memory_search). Это НЕ история сессий (она в session_turns и уходит в prompt
автоматически) — это заметки между сессиями: «у клиента X прод на .230»,
«аллергия на verbose-ответы». Таблица memories + FTS5 (schema v5).
"""
from __future__ import annotations

import re

from .db import get_db, now_iso


def save(note: str, tags: str = "") -> int:
    db = get_db()
    try:
        cur = db.execute("INSERT INTO memories (ts, note, tags) VALUES (?,?,?)",
                         (now_iso(), note, tags))
        mem_id = cur.lastrowid
        db.execute("INSERT INTO memories_fts (note, tags, mem_id) VALUES (?,?,?)",
                   (note, tags, str(mem_id)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return mem_id


def search(query: str, limit: int = 5) -> list[dict]:
    # слова в кавычках (AND): дефисы/скобки — синтаксис FTS5 (как в vault)
    fts = " ".join(f'"{t}"' for t in query.replace('"', " ").split()) or '""'
    rows = get_db().execute(
        """SELECT m.id, m.ts, m.note, m.tags
           FROM memories_fts f JOIN memories m ON m.id = CAST(f.mem_id AS INTEGER)
           WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?""",
        (fts, limit)).fetchall()
    return [dict(r) for r in rows]


def recent(limit: int = 20) -> list[dict]:
    rows = get_db().execute(
        "SELECT id, ts, note, tags FROM memories ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def search_any(query: str, limit: int = 5) -> list[dict]:
    """OR-поиск по токенам запроса: живой вопрос редко совпадает с заметкой
    слово в слово, поэтому здесь OR, а не AND как в search() (memory_search)."""
    toks = [t for t in re.findall(r"[a-zа-яё0-9_]{4,}", (query or "").lower())][:8]
    if not toks:
        return []
    fts = " OR ".join(f'"{t}"' for t in toks)
    rows = get_db().execute(
        """SELECT m.id, m.ts, m.note, m.tags
           FROM memories_fts f JOIN memories m ON m.id = CAST(f.mem_id AS INTEGER)
           WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?""",
        (fts, limit)).fetchall()
    return [dict(r) for r in rows]


def session_digest(sid: str) -> str | None:
    """Плывущий конспект сессии (одна строка, обновляется по мере разговора)."""
    row = get_db().execute(
        "SELECT note FROM memories WHERE tags=? ORDER BY id DESC LIMIT 1",
        (f"auto-sid:{sid}",)).fetchone()
    return row["note"] if row else None


def upsert_session_digest(sid: str, note: str) -> int:
    db = get_db()
    tag = f"auto-sid:{sid}"
    try:
        row = db.execute(
            "SELECT id, note, tags FROM memories WHERE tags=? ORDER BY id DESC LIMIT 1",
            (tag,)).fetchone()
        if row:
            # memories_fts — обычная contentful FTS5 (без content=): команда
            # 'delete' валидна только для external-content, здесь она падала
            # SQL logic error, а транзакция без rollback оставалась открытой —
            # перманентный write-лок в конце каждого хода (инцидент 2026-10-30).
            # У contentful таблицы старая строка индекса снимается обычным DELETE.
            db.execute("DELETE FROM memories_fts WHERE mem_id=?", (str(row["id"]),))
            db.execute("UPDATE memories SET note=?, ts=? WHERE id=?",
                       (note, now_iso(), row["id"]))
            db.execute("INSERT INTO memories_fts (note, tags, mem_id) VALUES (?,?,?)",
                       (note, tag, str(row["id"])))
            db.commit()
            return row["id"]
        return save(note, tags=tag)
    except Exception:
        db.rollback()
        raise


RECALL_HEADER = ("Память разговоров (конспекты сессий и факты; важное "
                 "закрепляй memory_save):\n")


def recall_block(prompt: str, limit: int = 5, recent_n: int = 3,
                 cap: int = 2000) -> str:
    """Блок «Память разговоров» в системный промпт: FTS-хиты по текущему вопросу
    плюс свежие заметки. Пусто — пустая строка, блок не подмешивается."""
    seen: set = set()
    items: list = []
    for m in search_any(prompt, limit) + recent(recent_n):
        if m["id"] not in seen:
            seen.add(m["id"])
            items.append(m)
    if not items:
        return ""
    body = "\n".join(f"- {m['note']}" for m in items)
    if len(body) > cap:
        body = body[:cap] + "\n[…память обрезана: ищи точнее через memory_search]"
    return RECALL_HEADER + body
