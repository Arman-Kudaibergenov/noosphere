"""vault.py — хранилище доступов контура (батч «vault», 2026-10-11).

Зачем: агенты гадали, где ssh-алиасы, токены, MCP-эндпоинты и логины — и
путали. Теперь один ответ: vault_search → метаданные БЕЗ секрета, vault_get
с reveal=true → секрет по явному запросу. Секрет в поиск не попадает никогда:
меньше утечек в контекст модели и в историю сессий (tool-результаты в
снапшоты не пишутся, но дисциплина дешевле надежды).

Канон хозяйства сохраняется: предпочтительно secret_file (путь к файлу из
~/.secrets), значение secret — для того, чего в файлах нет. kind: ssh | mcp |
token | login | service | skill | note. UNIQUE(kind, name) — upsert.

G1.5 (аудит r3): значение secret шифруется at-rest (Fernet, cryptography).
Ключ — ~/.secrets/noosphere-vault.key (server.vault_key_file), вне джейла
инструментов; при первом обращении генерируется (НЕ в репо). Ленивая миграция:
строка не похожа на fernet-токен → при чтении отдаётся как есть и переписывается
зашифрованной. Без доступа к ключу расшифровка невозможна — секрет вернётся
как «<зашифровано, нет ключа>».
"""
from __future__ import annotations

from pathlib import Path

from .config import load_config
from .db import get_db, now_iso

_KINDS = ("ssh", "mcp", "token", "login", "service", "skill", "note")
# Публичная выдача (list/search) — БЕЗ secret, notes и secret_file (F11.3 аудита
# r3: notes может содержать чувствительное, а путь к секрет-файлу + read_file был
# обходом reveal). notes/secret_file остаются в vault.get по явному запросу.
_PUB = ("v.id, v.kind, v.name, v.host, v.login, v.tags, v.company, v.updated_at,"
        " (v.secret IS NOT NULL AND v.secret != '') AS has_secret")

_FERNET_PREFIX = "gAAAA"  # все Fernet-токены начинаются с этого base64-префикса


def _fernet():
    """Fernet по ключу из vault_key_file; файла нет — генерируем (0600)."""
    from cryptography.fernet import Fernet

    p = Path(load_config().server.vault_key_file).expanduser()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(Fernet.generate_key().decode(), encoding="utf-8")
        p.chmod(0o600)
        print(f"[vault] сгенерирован новый ключ {p}")
    return Fernet(p.read_text(encoding="utf-8").strip().encode())


def _encrypt(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def _decrypt(value: str) -> str:
    """Расшифровка fernet-значения; не fernet — вернуть как есть (легаси)."""
    if not value.startswith(_FERNET_PREFIX):
        return value
    try:
        return _fernet().decrypt(value.encode()).decode()
    except Exception:
        return "<зашифровано, нет ключа или битое значение>"


def _maybe_migrate_plain(entry_id: int, value: str) -> None:
    """Ленивая миграция открытого текста → fernet при первом чтении."""
    if not value or value.startswith(_FERNET_PREFIX):
        return
    try:
        db = get_db()
        db.execute("UPDATE vault SET secret=? WHERE id=?", (_encrypt(value), entry_id))
        db.commit()
    except Exception as e:
        print(f"[vault] ленивая миграция секрета #{entry_id} не удалась: {e!r}")


def upsert(kind: str, name: str, host: str = "", login: str = "", secret: str = "",
           secret_file: str = "", notes: str = "", tags: str = "", company: str = "") -> int:
    if kind not in _KINDS:
        raise ValueError(f"kind должен быть одним из {_KINDS}")
    db = get_db()
    stored_secret = _encrypt(secret) if secret else ""
    db.execute(
        """INSERT INTO vault (kind, name, host, login, secret, secret_file, notes, tags,
                               company, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(kind, name) DO UPDATE SET
             host=excluded.host, login=excluded.login,
             secret=CASE WHEN excluded.secret != '' THEN excluded.secret ELSE vault.secret END,
             secret_file=CASE WHEN excluded.secret_file != '' THEN excluded.secret_file ELSE vault.secret_file END,
             notes=excluded.notes, tags=excluded.tags, company=excluded.company,
             updated_at=excluded.updated_at""",
        (kind, name, host, login, stored_secret, secret_file, notes, tags, company,
         now_iso()))
    db.commit()
    row = db.execute("SELECT id FROM vault WHERE kind=? AND name=?", (kind, name)).fetchone()
    entry_id = row["id"]
    db.execute("DELETE FROM vault_fts WHERE entry_id=?", (str(entry_id),))
    db.execute("INSERT INTO vault_fts (name, host, login, notes, tags, entry_id)"
               " VALUES (?,?,?,?,?,?)",
               (name, host, login, notes, tags, str(entry_id)))
    db.commit()
    return entry_id


def _fts_query(query: str) -> str:
    """Слова запроса — в кавычках (AND): дефисы/скобки — синтаксис FTS5."""
    return " ".join(f'"{t}"' for t in query.replace('"', " ").split()) or '""'


def search(query: str, limit: int = 10) -> list[dict]:
    """Полнотекстовый поиск — ТОЛЬКО метаданные, без поля secret."""
    rows = get_db().execute(
        f"""SELECT {_PUB} FROM vault v
            JOIN vault_fts f ON CAST(f.entry_id AS INTEGER) = v.id
            WHERE vault_fts MATCH ? ORDER BY rank LIMIT ?""",
        (_fts_query(query), limit)).fetchall()
    return [dict(r) for r in rows]


def get(entry_id: int) -> dict | None:
    """Полная запись, включая secret — только по явному vault_get.
    secret возвращается РАСШИФРОВАННЫМ (at-rest — fernet, G1.5); легаси-строки
    открытого текста мигрируют в шифрованные при этом чтении."""
    row = get_db().execute("SELECT * FROM vault WHERE id=?", (entry_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d.get("secret"):
        _maybe_migrate_plain(entry_id, d["secret"])
        d["secret"] = _decrypt(d["secret"])
    return d


def list_recent(limit: int = 50) -> list[dict]:
    rows = get_db().execute(
        f"SELECT {_PUB} FROM vault v ORDER BY v.updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def delete(entry_id: int) -> bool:
    db = get_db()
    cur = db.execute("DELETE FROM vault WHERE id=?", (entry_id,))
    db.execute("DELETE FROM vault_fts WHERE entry_id=?", (str(entry_id),))
    db.commit()
    return cur.rowcount > 0


def list_for_owner(limit: int = 200) -> list[dict]:
    """Полная выдача ВЛАДЕЛЬЦУ (Bearer): company, notes и secret_file включены.

    Для модели этот слой недоступен: vault_search/list_recent остаются на _PUB
    без notes и secret_file (F11.3 аудита r3 — notes + read_file был обходом reveal).
    """
    rows = get_db().execute(
        "SELECT id, kind, name, host, login, tags, company, notes, secret_file,"
        " updated_at, (secret IS NOT NULL AND secret != '') AS has_secret"
        " FROM vault ORDER BY company, kind, name LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def set_company(entry_id: int, company: str) -> bool:
    db = get_db()
    cur = db.execute("UPDATE vault SET company=?, updated_at=? WHERE id=?",
                     (company.strip(), now_iso(), entry_id))
    db.commit()
    return cur.rowcount > 0


def reveal_for_owner(entry_id: int, max_bytes: int = 65536) -> dict | None:
    """Reveal для ВЛАДЕЛЬЦА: секрет из БД или, если его там нет, из secret_file.

    Чтение файла ограничено домашним каталогом и 64 КБ: vault хранит указатели на
    ~/.secrets и подобные файлы, а не произвольные пути. След — в tool_audit
    (вызывает main). Для модели этот путь недоступен: vault_get отдаёт secret_file
    как путь, без содержимого (F11.3 аудита r3).
    """
    d = get(entry_id)
    if d is None:
        return None
    out = {k: d.get(k) for k in ("id", "kind", "name", "host", "login", "company",
                                 "notes", "secret_file", "secret")}
    out["secret_source"] = "db" if out["secret"] else ""
    if not out["secret"] and out["secret_file"]:
        p = Path(out["secret_file"]).expanduser()
        try:
            if p.is_file() and p.stat().st_size <= max_bytes and p.is_relative_to(Path.home()):
                out["secret"] = p.read_text(encoding="utf-8", errors="replace").strip()
                out["secret_source"] = "file"
            else:
                out["secret_source"] = "file-out-of-bounds"
        except OSError:
            out["secret_source"] = "file-unreadable"
    return out
