#!/usr/bin/env python3
"""ingest_external_burn.py — ингест чужих леджеров расхода в turns (A4 аудита).

Прибор «Ноосферы» видит только свой трафик; реальный расход идёт через
BioClaude (~/bc-personal-data/usage_log-*.jsonl, есть cost_usd) и taskq
(~/tasks/telemetry.jsonl, cost_usd_est). Этот скрипт перекладывает оба
источника в turns с каналами bioclaude:<provider> и taskq, чтобы /v1/burn
отвечал на денежный вопрос по всему хозяйству, а не по 13 своим вызовам.

Идемпотентно: смещение (число обработанных строк) хранится в meta
(ingest_offset:<источник>) — быстрый путь; плюс защита от дублей (G10):
таблица ingest_seen(source, sha256(строки)) — повтор строки при ротации
источника или восстановлении БД из бэкапа не создаст вторую запись turns.
Файл уменьшился (ротация) — смещение сбрасывается, dedupe остаётся.
Запуск: ./.venv/bin/python scripts/ingest_external_burn.py (cron */10).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import telemetry  # noqa: E402
from server.config import load_config  # noqa: E402
from server.db import get_db  # noqa: E402

SOURCES = [
    ("bioclaude", Path.home() / "bc-personal-data"),
    ("taskq", Path.home() / "tasks" / "telemetry.jsonl"),
]


def _offset(db, key: str) -> int:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return int(row[0]) if row else 0


def _set_offset(db, key: str, n: int) -> None:
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, str(n)))


def _seen_or_mark(db, source: str, line: str) -> bool:
    """True — строка уже ингестирована (дубль). Иначе помечает и False.
    SELECT до INSERT: дубль не должен открывать write-транзакцию — незакрытый
    INSERT держит write-лок SQLite на всё время скана источника."""
    h = hashlib.sha256(line.encode()).hexdigest()
    if db.execute("SELECT 1 FROM ingest_seen WHERE source=? AND line_hash=?",
                  (source, h)).fetchone():
        return True
    db.execute("INSERT OR IGNORE INTO ingest_seen (source, line_hash, ts)"
               " VALUES (?,?,?)",
               (source, h, dt.datetime.now(dt.timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z")))
    db.commit()  # короткая транзакция: не держим write-лок на весь скан
    return False


def _day_of(ts: dt.datetime, tz_hours: int) -> str:
    tz = dt.timezone(dt.timedelta(hours=tz_hours))
    return ts.astimezone(tz).date().isoformat()


def _parse_ts(v: str) -> dt.datetime | None:
    try:
        d = dt.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def ingest_bioclaude(db, folder: Path, tz_hours: int) -> int:
    n_new = 0
    for path in sorted(folder.glob("usage_log-*.jsonl")):
        key = f"ingest_offset:bioclaude:{path.name}"
        done = _offset(db, key)
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) < done:
            done = 0
        for line in lines[done:]:
            if _seen_or_mark(db, f"bioclaude:{path.name}", line):
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("model") == "<synthetic>":
                continue
            ts = _parse_ts(r.get("ts", ""))
            if ts is None:
                continue
            provider = r.get("provider") or "unknown"
            telemetry.record_turn(telemetry.TurnRec(
                session_id=r.get("session_id"), user=r.get("login") or "bioclaude",
                task_class="external", model=r.get("model") or "",
                channel=f"bioclaude:{provider}",
                ok=not r.get("is_error"), answered=not r.get("is_error"),
                err=(r.get("error") or "")[:400],
                prompt_tokens=int(r.get("input_tokens") or 0),
                completion_tokens=int(r.get("output_tokens") or 0),
                cache_read_tokens=int(r.get("cache_read_tokens") or 0),
                ms=int(r.get("duration_ms") or 0),
                cost_usd=float(r.get("cost_usd") or 0.0),
                ts=ts.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                day=_day_of(ts, tz_hours)))
            n_new += 1
        _set_offset(db, key, len(lines))
        db.commit()
    return n_new


def ingest_taskq(db, path: Path, tz_hours: int) -> int:
    if not path.exists():
        return 0
    key = "ingest_offset:taskq"
    done = _offset(db, key)
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < done:
        done = 0
    n_new = 0
    for line in lines[done:]:
        if _seen_or_mark(db, "taskq", line):
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_ts(r.get("ts", ""))
        if ts is None:
            continue
        telemetry.record_turn(telemetry.TurnRec(
            user=r.get("agent") or "taskq", task_class="external",
            model=r.get("model") or "", channel="taskq",
            ok=(r.get("rc") == 0), answered=(r.get("rc") == 0),
            prompt_tokens=int(r.get("in_tokens") or 0),
            completion_tokens=int(r.get("out_tokens") or 0),
            ms=int(float(r.get("duration_s") or 0) * 1000),
            cost_usd=float(r.get("cost_usd_est") or 0.0),
            ts=ts.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            day=_day_of(ts, tz_hours)))
        n_new += 1
    _set_offset(db, key, len(lines))
    db.commit()
    return n_new


def main() -> None:
    import fcntl
    lock_fh = Path("/tmp/noosphere-ingest.lock").open("w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("ingest skip: параллельный запуск держит лок")
        return
    cfg = load_config()
    tz_hours = cfg.server.tz_offset_hours
    db = get_db()
    total = 0
    for name, src in SOURCES:
        if name == "bioclaude" and src.is_dir():
            total += ingest_bioclaude(db, src, tz_hours)
        elif name == "taskq":
            total += ingest_taskq(db, src, tz_hours)
    print(f"ingest ok: +{total} записей")


if __name__ == "__main__":
    main()
