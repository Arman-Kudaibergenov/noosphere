"""projects.py — саморастущие проекты Noosphere (батч 6).

Проект = именованный контур работ (папка сессий + ключ классификации /v1/route).
Стартовый сид — ключевые слова владельца; классификатор может создать новый
проект сам (auto=1) — список растёт без ручного заведения.

Ключевые слова — бесплатный сигнал: match_by_keywords бежит ДО вызова LLM.
Переименование проекта НЕ трогает folder существующих сессий — папки уже
разложенных сессий остаются как были, новое имя получают только новые сессии.
"""
from __future__ import annotations

from .db import get_db, now_iso, today_local

SEED_DEFAULTS: list[tuple[str, str]] = [
    ("Ноосфера", "ноосфера,noosphere"),
    ("Личное", "личное"),
]


def _seed_list() -> list[tuple[str, str]]:
    """Сиды проектов: нейтральный дефолт + список из конфига (projects.seed:
    [{name, keywords}]). Проектные контуры владельца (заказчики, компании)
    живут в конфиге инстанса, не в коде."""
    from .config import load_config
    extra = [(str(p.get("name", "")).strip(), str(p.get("keywords", "")).strip())
             for p in load_config().projects_seed if isinstance(p, dict)]
    return SEED_DEFAULTS + [p for p in extra if p[0]]


def _norm(name: str) -> str:
    return " ".join((name or "").split()).strip()


def seed_defaults() -> int:
    """Идемпотентный сид стартовых проектов (вызывается из startup).
    Существующие имена не трогаем — ключевые слова владелец мог править."""
    db = get_db()
    n = 0
    for name, keywords in _seed_list():
        cur = db.execute(
            "INSERT OR IGNORE INTO projects (name, keywords, auto, created_at)"
            " VALUES (?,?,0,?)",
            (name, keywords, now_iso()))
        n += cur.rowcount
    db.commit()
    return n


def get_by_name(name: str) -> dict | None:
    # SQLite lower() не складывает кириллицу — сравнение на стороне Python
    target = _norm(name).casefold()
    for row in get_db().execute("SELECT * FROM projects").fetchall():
        if row["name"].casefold() == target:
            return dict(row)
    return None


def get(project_id: int) -> dict | None:
    row = get_db().execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return dict(row) if row else None


def auto_count() -> int:
    """Число auto-проектов (созданных классификатором) — для потолка G3."""
    row = get_db().execute("SELECT COUNT(*) c FROM projects WHERE auto=1").fetchone()
    return int(row["c"])


def find_or_create(name: str) -> dict:
    """Проект по имени (case-insensitive); нет — создаёт auto=1 с именем как
    дал классификатор (осмысленное имя ценнее шаблона «Новый проект: …»).
    Параллельный дубль (IntegrityError на UNIQUE) — повторный поиск (G3)."""
    import sqlite3

    name = _norm(name) or "Без проекта"
    row = get_by_name(name)
    if row:
        return row
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO projects (name, keywords, auto, created_at) VALUES (?,?,1,?)",
            (name, "", now_iso()))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        row = get_by_name(name)
        if row:
            return row
        raise
    row = get(cur.lastrowid)
    assert row is not None
    return row


def match_by_keywords(text: str) -> dict | None:
    """Проект по ключевым словам в тексте запроса (бесплатный сигнал до LLM).
    Совпадение: ключ — подстрока текста ИЛИ слово текста начинается с ключа
    (простая толерантность к русским окончаниям: «заказе» ~ «заказ» — только
    для ключей длиной ≥4). При совпадении нескольких — самый длинный ключ."""
    low = (text or "").lower()
    if not low:
        return None
    words_in_text = low.split()
    best: dict | None = None
    best_len = 0
    for row in get_db().execute("SELECT * FROM projects").fetchall():
        words = [w.strip().lower() for w in (row["keywords"] or "").split(",") if w.strip()]
        if row["name"].lower() not in words:
            words.append(row["name"].lower())
        for w in words:
            hit = w in low
            if not hit and len(w) >= 4:
                # русская морфология меняет последнюю букву (заказ→заказе):
                # сравниваем по основе ключа без финальной буквы
                stem = w[:-1]
                hit = any(t.startswith(stem) for t in words_in_text)
            if hit and len(w) > best_len:
                best, best_len = dict(row), len(w)
    return best


def list_projects() -> list[dict]:
    """Проекты + число сессий в одноимённой папке (folder = name проекта)."""
    db = get_db()
    rows = db.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM sessions s WHERE s.folder = p.name)"
        " AS sessions_count FROM projects p ORDER BY p.auto, p.name").fetchall()
    return [dict(r) for r in rows]


def add(name: str, keywords: str = "") -> dict:
    row = find_or_create(name)
    if keywords:
        db = get_db()
        merged = ",".join(w.strip() for w in (row["keywords"] + "," + keywords).split(",")
                          if w.strip())
        db.execute("UPDATE projects SET keywords=? WHERE id=?", (merged, row["id"]))
        db.commit()
        row = get(row["id"]) or row
    return row


def rename(project_id: int, name: str) -> dict | None:
    """Переименование. Папки существующих сессий НЕ переименовываются —
    новое имя применяется только к новым сессиям (осознанный шов)."""
    name = _norm(name)
    if not name:
        raise ValueError("пустое имя проекта")
    db = get_db()
    clash = get_by_name(name)
    if clash and clash["id"] != project_id:
        raise ValueError("проект с таким именем уже есть")
    cur = db.execute("UPDATE projects SET name=? WHERE id=?", (name, project_id))
    db.commit()
    return get(project_id) if cur.rowcount else None
