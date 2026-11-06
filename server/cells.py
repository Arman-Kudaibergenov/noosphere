"""cells.py — реестр ячеек (принцип nodecode.ai, v20 2026-10-28).

Ячейка = способность агента: скил на диске (SKILL.md), MCP-сервер, встроенный
тул (tools.py) или ячейка, объявленная самим агентом (kind='custom', таблица
cells). Живые ячейки сканируются в момент вызова — новый скил/MCP/тул виден
без рестарта сервера, объявленные живут в SQLite. Единый реестр отвечает на
вопрос «что у меня есть и чем я могу пользоваться», это основа
самодоработки: сначала объявить ячейку, потом ею пользоваться.
"""
from __future__ import annotations

import time

from .config import load_config
from .db import get_db

KINDS = ("skill", "mcp", "tool", "custom")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def live_skills() -> list[dict]:
    """Скилы с диска: glob на месте вызова => хотлоад без рестарта."""
    from . import skills

    out = []
    for row in skills.catalog():
        out.append({
            "kind": "skill",
            "name": row["name"],
            "descr": row.get("desc", ""),
            "enabled": bool(row.get("enabled", True)),
            "how": "skill_load(name)",
            "source": "disk",
        })
    return out


def live_mcp() -> list[dict]:
    """MCP-серверы из конфига (mcp.servers)."""
    out = []
    try:
        servers = load_config().mcp.servers or {}
    except Exception:
        return out
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        out.append({
            "kind": "mcp",
            "name": name,
            "descr": spec.get("desc", "") or "",
            "enabled": bool(spec.get("enabled", True)),
            "how": "mcp_tools(server) / mcp_call(server, tool, args)",
            "source": spec.get("url", ""),
        })
    return out


def live_tools() -> list[dict]:
    """Встроенные тулы: имена и описания из openai_specs (без параметров)."""
    from . import tools

    out = []
    for spec in tools.openai_specs():
        fn = spec.get("function", spec)
        name = fn.get("name", "")
        if not name:
            continue
        descr = (fn.get("description", "") or "").split(". ")[0][:160]
        out.append({
            "kind": "tool",
            "name": name,
            "descr": descr,
            "enabled": True,
            "how": "вызов напрямую",
            "source": "tools.py",
        })
    return out


def declared(kind: str | None = None) -> list[dict]:
    """Ячейки, объявленные агентом/владельцем (таблица cells)."""
    sql = "SELECT name, kind, descr, how, enabled, created_at, updated_at FROM cells"
    args: list = []
    if kind:
        sql += " WHERE kind = ?"
        args.append(kind)
    sql += " ORDER BY name"
    out = []
    for r in get_db().execute(sql, args).fetchall():
        out.append({
            "kind": r["kind"],
            "name": r["name"],
            "descr": r["descr"] or "",
            "enabled": bool(r["enabled"]),
            "how": r["how"] or "",
            "source": "declared",
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return out


def registry(kind: str | None = None) -> dict[str, list[dict]]:
    """Полный реестр по видам: skill/mcp/tool/custom."""
    data = {
        "skill": live_skills(),
        "mcp": live_mcp(),
        "tool": live_tools(),
        "custom": declared("custom"),
    }
    if kind:
        return {kind: data.get(kind, [])}
    return data


def all_cells() -> list[dict]:
    rows: list[dict] = []
    for k in KINDS:
        rows.extend(registry(k).get(k, []))
    return rows


def get(name: str) -> dict | None:
    for row in all_cells():
        if row["name"] == name:
            return row
    return None


def upsert(name: str, *, descr: str = "", how: str = "", kind: str = "custom",
           enabled: bool = True) -> dict:
    """Объявить/обновить ячейку. Живые ячейки (skill/mcp/tool) не перетираем."""
    name = (name or "").strip()
    if not name:
        raise ValueError("имя ячейки пустое")
    kind = (kind or "custom").strip().lower()
    if kind != "custom":
        raise ValueError("объявлять можно только kind='custom'")
    now = _now()
    db = get_db()
    row = db.execute("SELECT name FROM cells WHERE name = ?", (name,)).fetchone()
    if row:
        db.execute(
            "UPDATE cells SET descr = ?, how = ?, enabled = ?, updated_at = ? "
            "WHERE name = ?",
            (descr, how, 1 if enabled else 0, now, name),
        )
    else:
        db.execute(
            "INSERT INTO cells (name, kind, descr, how, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, kind, descr, how, 1 if enabled else 0, now, now),
        )
    db.commit()
    return get(name) or {"name": name, "kind": kind}


def set_enabled(name: str, enabled: bool) -> bool:
    """Включить/выключить ячейку. Скилы — через skills.skill_state, прочие — в cells."""
    from . import skills

    if any(row["name"] == name for row in live_skills()):
        return skills.set_enabled(name, enabled)
    db = get_db()
    cur = db.execute(
        "UPDATE cells SET enabled = ?, updated_at = ? WHERE name = ?",
        (1 if enabled else 0, _now(), name),
    )
    db.commit()
    return cur.rowcount > 0


def render(kind: str | None = None, limit: int = 400) -> str:
    """Текстовое представление реестра для модели."""
    data = registry(kind)
    lines: list[str] = []
    titles = {"skill": "Скилы", "mcp": "MCP-серверы", "tool": "Встроенные тулы",
              "custom": "Объявленные ячейки"}
    for k, rows in data.items():
        if not rows:
            continue
        rows = rows[:limit]
        lines.append(f"== {titles.get(k, k)} ({len(rows)}) ==")
        for r in rows:
            flag = "" if r.get("enabled", True) else " [выкл]"
            descr = (r.get("descr") or "").strip()
            lines.append(f"- {r['name']}{flag}" + (f" — {descr}" if descr else ""))
    if not lines:
        return "реестр ячеек пуст"
    return "\n".join(lines)


def summary() -> dict:
    """Счётчики по видам — для /health и окна."""
    data = registry()
    return {k: len(v) for k, v in data.items()}


def forget(name: str) -> bool:
    """Удалить объявленную ячейку (живые скилы/MCP/тулы не трогаем)."""
    db = get_db()
    cur = db.execute("DELETE FROM cells WHERE name = ? AND kind = 'custom'", (name,))
    db.commit()
    return cur.rowcount > 0
