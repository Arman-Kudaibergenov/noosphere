"""Read-only ингест логов харнесов (`usage_record.jsonl`) — второй контур данных аудита.

Строка лога Qwen/Kimi = сессия: по моделям requests/input/output/cached, `tools.byName`,
`skills.byName`, `files.linesAdded/linesRemoved`. Здесь она становится строкой
`harness_sessions` с дедупом через `ingest_seen` (source+sha256 строки) — повторно
 ingested-файл не плодит дубли. В headline-цифры Ноосферы эти данные не смешиваются:
source='harness' живёт отдельной таблицей и отдельной секцией отчёта.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from . import db


def line_hash(row: dict) -> str:
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _num(*vals) -> int:
    for v in vals:
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def ingest_rows(rows: list, source: str = "harness") -> dict:
    c = db.get_db()
    added = skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        lh = line_hash(row)
        if c.execute("SELECT 1 FROM ingest_seen WHERE source=? AND line_hash=?",
                     (source, lh)).fetchone():
            skipped += 1
            continue
        per = row.get("models") or row.get("perModel") or {}
        req = inp = outp = cached = 0
        dominant, best = "", -1
        if isinstance(per, dict):
            for name, m in per.items():
                if not isinstance(m, dict):
                    continue
                r = _num(m.get("requests"))
                req += r
                inp += _num(m.get("inputTokens"), m.get("input"))
                outp += _num(m.get("outputTokens"), m.get("output"))
                cached += _num(m.get("cachedTokens"), m.get("cached"))
                if r > best:
                    best, dominant = r, str(name)
        ts_ms = _num(row.get("timestamp"))
        day = (datetime.fromtimestamp(ts_ms / 1000, timezone.utc).date().isoformat()
               if ts_ms else str(row.get("localDate") or row.get("day") or "")[:10])
        tools = (row.get("tools") or {}).get("byName") or row.get("tools") or {}
        skills = (row.get("skills") or {}).get("byName") or row.get("skills") or {}
        files = row.get("files") or {}
        c.execute(
            "INSERT INTO harness_sessions(source, session_id, day, model, requests,"
            " input_tokens, output_tokens, cached_tokens, tools_json, skills_json,"
            " lines_added, lines_removed, ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source, str(row.get("sessionId") or row.get("session_id") or ""),
             day, dominant, req, inp, outp, cached,
             json.dumps(tools, ensure_ascii=False)[:2000],
             json.dumps(skills, ensure_ascii=False)[:2000],
             _num(files.get("linesAdded")), _num(files.get("linesRemoved")),
             db.now_iso()))
        c.execute("INSERT OR IGNORE INTO ingest_seen(source, line_hash, ts) VALUES(?,?,?)",
                  (source, lh, db.now_iso()))
        added += 1
    c.commit()
    return {"added": added, "skipped": skipped}


def summary(days: int = 7, conn=None) -> list[dict]:
    c = conn or db.get_db()
    return [dict(r) for r in c.execute(
        "SELECT source, day, COUNT(*) AS sessions, SUM(requests) AS reqs,"
        " SUM(input_tokens) AS inp, SUM(output_tokens) AS outp,"
        " SUM(cached_tokens) AS cached, SUM(lines_added) AS added,"
        " SUM(lines_removed) AS removed FROM harness_sessions"
        f" WHERE day >= date('now', '-{int(days)} days') GROUP BY source, day"
        " ORDER BY day DESC LIMIT 60")]
