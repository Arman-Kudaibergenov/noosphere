"""uploads.py — вложения чанками (PROTOCOL v1.1).

Файл грузится сырыми байтами: init → chunk?offset=N (можно несколько) → done.
Модель последовательная: offset обязан равняться size_received, иначе 409 —
переупорядочивание чанков на сервере не собираем, это работа клиента.
В теле /v1/chat вложения идут только ссылками (upload_id), не base64.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import load_config
from .db import get_db, now_iso

router = APIRouter()


class UploadInit(BaseModel):
    name: str
    size: int
    mime: str = "application/octet-stream"
    session_id: str | None = None


def _uploads_dir() -> Path:
    p = load_config().data_dir / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get(uid: str):
    return get_db().execute("SELECT * FROM uploads WHERE id = ?", (uid,)).fetchone()


@router.post("/v1/uploads/init")
async def init_upload(inp: UploadInit):
    uid = str(uuid.uuid4())
    path = _uploads_dir() / uid
    path.touch()
    db = get_db()
    db.execute(
        "INSERT INTO uploads (id, ts, session_id, name, mime, size, size_received, path, status)"
        " VALUES (?,?,?,?,?,?,0,?,'init')",
        (uid, now_iso(), inp.session_id, inp.name, inp.mime, inp.size, str(path)),
    )
    db.commit()
    return {"upload_id": uid}


@router.put("/v1/uploads/{uid}/chunk")
async def upload_chunk(uid: str, request: Request, offset: int = 0):
    row = _get(uid)
    if row is None:
        raise HTTPException(404, "upload not found")
    if row["status"] != "init":
        raise HTTPException(409, "upload already done")
    if offset != row["size_received"]:
        raise HTTPException(409, f"offset mismatch: ожидался {row['size_received']}")
    data = await request.body()
    with open(row["path"], "r+b") as f:
        f.seek(offset)
        f.write(data)
    received = offset + len(data)
    db = get_db()
    db.execute("UPDATE uploads SET size_received = ? WHERE id = ?", (received, uid))
    db.commit()
    return {"received": received}


@router.post("/v1/uploads/{uid}/done")
async def upload_done(uid: str):
    row = _get(uid)
    if row is None:
        raise HTTPException(404, "upload not found")
    size = Path(row["path"]).stat().st_size
    db = get_db()
    db.execute("UPDATE uploads SET status = 'done', size = ? WHERE id = ?", (size, uid))
    db.commit()
    return {"ok": True, "size": size}


@router.get("/v1/uploads/{uid}")
async def get_upload(uid: str):
    row = _get(uid)
    if row is None or row["status"] != "done":
        raise HTTPException(404, "upload not found")
    return FileResponse(row["path"], media_type=row["mime"] or "application/octet-stream",
                        filename=row["name"], content_disposition_type="inline")


def resolve_attachments(ids: list[str]) -> list[dict]:
    """Проверяет, что вложения существуют и догружены; возвращает
    [{upload_id, name, mime}] для снапшота хода. ValueError — на вход клиента."""
    out = []
    db = get_db()
    for uid in ids:
        row = db.execute("SELECT id, name, mime, status FROM uploads WHERE id = ?",
                         (uid,)).fetchone()
        if row is None:
            raise ValueError(f"вложение {uid} не найдено")
        if row["status"] != "done":
            raise ValueError(f"вложение {row['name'] or uid} ещё не загружено")
        out.append({"upload_id": row["id"], "name": row["name"], "mime": row["mime"]})
    return out


# v1.6: вложения доходят до модели. Текст — инлайн-блоком в промпт,
# изображения — мультимодальным content-массивом (OpenAI image_url / ollama
# images), остальное — честным описанием (имя, тип, размер), как раньше.
TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml", ".xml",
             ".py", ".js", ".ts", ".html", ".css", ".sh", ".sql", ".ini",
             ".cfg", ".toml", ".env.example"}
IMG_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
TEXT_MAX = 50 * 1024            # содержимое текстового вложения после обрезки
IMG_MAX = 4 * 1024 * 1024       # одна картинка (ресайз не делаем — отказ)
IMG_MAX_N = 3                   # картинок на ход
IMG_B64_TOTAL_MAX = 10 * 1024 * 1024  # суммарный base64 на ход


def build_attachment_parts(ids: list[str]) -> dict:
    """Резолв + сборка контента для модели.
    → {meta, text_blocks, images, notes}:
    meta — [{upload_id, name, mime}] для снапшота хода;
    text_blocks — «Вложение <имя>:\\n<содержимое ≤50 КБ>»;
    images — [{name, mime, b64}] с лимитами IMG_MAX_N/IMG_MAX/IMG_B64_TOTAL_MAX;
    notes — строки-описания бинарных/отклонённых (попадают в промпт скобкой).
    ValueError — на вход клиента (нет/недогружено)."""
    import base64 as _b64
    out = {"meta": [], "text_blocks": [], "images": [], "notes": []}
    db = get_db()
    b64_total = 0
    for uid in ids:
        row = db.execute("SELECT * FROM uploads WHERE id = ?", (uid,)).fetchone()
        if row is None:
            raise ValueError(f"вложение {uid} не найдено")
        if row["status"] != "done":
            raise ValueError(f"вложение {row['name'] or uid} ещё не загружено")
        name = row["name"] or uid
        mime = row["mime"] or "application/octet-stream"
        out["meta"].append({"upload_id": row["id"], "name": row["name"], "mime": mime})
        size = Path(row["path"]).stat().st_size
        if mime in IMG_MIMES:
            if size > IMG_MAX:
                out["notes"].append(
                    f"{name} ({mime}, {size // 1024} КБ) — картинка больше 4 МБ, "
                    "не ушла (ресайза нет)")
            elif len(out["images"]) >= IMG_MAX_N:
                out["notes"].append(f"{name} — лимит {IMG_MAX_N} картинки на ход, не ушла")
            else:
                b64 = _b64.b64encode(Path(row["path"]).read_bytes()).decode()
                if b64_total + len(b64) > IMG_B64_TOTAL_MAX:
                    out["notes"].append(f"{name} — суммарный лимит base64 10 МБ, не ушла")
                else:
                    b64_total += len(b64)
                    out["images"].append({"name": name, "mime": mime, "b64": b64})
            continue
        ext = Path(name).suffix.lower()
        if mime.startswith("text/") or ext in TEXT_EXTS:
            raw = Path(row["path"]).read_bytes()[:TEXT_MAX + 1]
            text = raw.decode("utf-8", errors="replace")
            if len(raw) > TEXT_MAX:
                text = text[:TEXT_MAX] + "\n[…обрезано до 50 КБ]"
            out["text_blocks"].append(f"Вложение {name}:\n{text}")
        else:
            out["notes"].append(
                f"{name} ({mime}, {size // 1024} КБ) — бинарное, модели доступно "
                "только это описание")
    return out
