"""skills.py — каталог скилов агента с диска (1С-контур, дизайн 2026-10-13).

Скил = каталог <skills.dir>/<имя>/SKILL.md с YAML-frontmatter (name/description).
Сканирование — по месту вызова (glob дешёвый), содержимое кэшируется in-memory
с инвалидацией по mtime файла. Флаги enabled — в SQLite (skill_state, v19),
дефолт enabled. Модель видит каталог через skill_list/auto_include-инъекцию,
тело — через skill_load (cap ~12К символов).
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from .config import load_config
from .db import get_db

LOAD_CAP = 12_000        # символов тела SKILL.md для skill_load
# Потолки каталога, который инжектится в промпт (резка 02.11). На бою блок
# занимал 20 433 знака (100 строк по ~202) и лип на всю 1С-сессию — ~6,8k
# токенов на каждом ходе. Полное описание остаётся в skill_list, тело — в
# skill_load, поэтому в инъекции достаточно короткого признака темы.
BLOCK_DESC_CAP = 110     # знаков описания в строке инъекции
BLOCK_NAME_CAP = 40      # знаков имени в строке инъекции
BLOCK_MAX_ROWS = 100     # строк в инъекции
CATALOG_HEADER = ("# Доступны скилы 1С-разработки (полное описание — skill_list, "
                  "тело — skill_load):\n")
_DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)
_NAME_RE = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
_1C_TOPIC_RE = re.compile(
    r"(?i)(1[cс]|bsl|cfe|edt|ibcmd|qbasic|yaxunit|скд|бсп|зуп|тмс|erp|"
    r"конфигурац|расширени|проведени|регистр|документооборот|vanessa|ванесс|"
    r"управляем\w*\s+форм)")

_CACHE: dict[str, dict] = {}   # name -> {desc, path, mtime}
_CACHE_DIR: str = ""


def _parse_skill(path: Path) -> dict | None:
    try:
        st = path.stat()
        head = path.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    fm = head[3:head.find("---", 3)] if "---" in head[3:] else head[3:]
    m = _NAME_RE.search(fm)
    name = (m.group(1).strip().strip("'\"") if m else "") or path.parent.name
    d = _DESC_RE.search(fm)
    desc = d.group(1).strip().strip("'\"").split(". ")[0][:200] if d else ""
    return {"name": name, "desc": desc, "path": str(path), "mtime": st.st_mtime}


def catalog() -> list[dict]:
    """Список скилов: name, desc (однострочник), enabled. Кэш с mtime-инвалидацией."""
    global _CACHE_DIR
    cfg = load_config()
    root = Path(cfg.skills.dir).expanduser()
    if str(root) != _CACHE_DIR:
        _CACHE.clear()
        _CACHE_DIR = str(root)
    if not root.is_dir():
        return []
    for f in root.glob("*/SKILL.md"):
        try:
            mt = f.stat().st_mtime
        except OSError:
            continue
        name = f.parent.name
        hit = _CACHE.get(name)
        if hit and hit["mtime"] == mt:
            continue
        parsed = _parse_skill(f)
        if parsed:
            _CACHE[parsed["name"]] = parsed
    states = {r["name"]: r["enabled"] for r in get_db().execute(
        "SELECT name, enabled FROM skill_state").fetchall()}
    return [{"name": s["name"], "desc": s["desc"], "enabled": states.get(s["name"], 1)}
            for s in sorted(_CACHE.values(), key=lambda x: x["name"])]


def load(name: str) -> str | None:
    """Тело SKILL.md по имени (cap LOAD_CAP). None — скил не найден."""
    catalog()  # обновить кэш по mtime
    hit = _CACHE.get(name)
    if hit is None:
        return None
    try:
        text = Path(hit["path"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) > LOAD_CAP:
        text = text[:LOAD_CAP] + f"\n[…обрезано, было {len(text)} символов]"
    return text


def set_enabled(name: str, enabled: bool) -> bool:
    """Переключить скил. False — такого скила нет на диске."""
    if not any(s["name"] == name for s in catalog()):
        return False
    db = get_db()
    db.execute("INSERT OR REPLACE INTO skill_state(name, enabled) VALUES(?,?)",
               (name, int(enabled)))
    db.commit()
    return True


def is_enabled(name: str) -> bool:
    row = get_db().execute("SELECT enabled FROM skill_state WHERE name=?",
                           (name,)).fetchone()
    return bool(row["enabled"]) if row else True


def auto_block(prompt: str | None = None) -> str:
    """Краткий каталог скилов по паттернам skills.auto_include.
    Без prompt — legacy-режим без тематического фильтра; с prompt — только
    для явной 1С-темы, иначе каталог не расходует токены не-1С хода."""
    if prompt is not None and not _1C_TOPIC_RE.search(prompt):
        return ""
    pats = load_config().skills.auto_include or []
    if not pats:
        return ""
    rows = [s for s in catalog()
            if s["enabled"] and any(fnmatch.fnmatchcase(s["name"], p) for p in pats)]
    if not rows:
        return ""
    return CATALOG_HEADER + _catalog_body(rows[:BLOCK_MAX_ROWS])


def _catalog_body(rows: list[dict]) -> str:
    return "\n".join(f"- {s['name'][:BLOCK_NAME_CAP]}: {s['desc'][:BLOCK_DESC_CAP]}"
                     for s in rows)


def worst_case_block(rows: int = BLOCK_MAX_ROWS) -> str:
    """Каталог худшего случая для гейта smoke: строки максимальной длины, которую
    допускают потолки. Рост инъекции виден без настоящих скилов на диске."""
    fake = [{"name": "n" * BLOCK_NAME_CAP, "desc": "d" * BLOCK_DESC_CAP}
            for _ in range(rows)]
    return CATALOG_HEADER + _catalog_body(fake)
