"""Ингест usage_record.jsonl харнеса в Ноосферу: файл читается read-only, дедуп на сервере.

Запуск:
    python scripts/ingest_harness_usage.py [--file PATH] [--server URL] [--source harness]
По умолчанию файл — ~/.qwen/usage_record.jsonl, сервер — LAN-адрес Мака.
Строки уходят батчами по 200 в POST /v1/audit/harness_ingest (Bearer из файла токена);
повторный прогон того же файла ничего не добавляет (ingest_seen на сервере).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=str(Path.home() / ".qwen" / "usage_record.jsonl"))
    ap.add_argument("--server", default="http://192.168.0.235:8300")
    ap.add_argument("--source", default="harness")
    a = ap.parse_args()

    path = Path(a.file)
    if not path.exists():
        print(f"ingest: файла {path} нет")
        return 1
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            obj["_source_file"] = a.source
            rows.append(obj)
    token = (Path.home() / ".secrets" / "noosphere.token").read_text(encoding="utf-8").strip()
    added = skipped = 0
    for i in range(0, len(rows), 200):
        batch = rows[i:i + 200]
        req = urllib.request.Request(
            a.server.rstrip("/") + "/v1/audit/harness_ingest",
            data=json.dumps(batch, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read().decode())
        added += int(out.get("added") or 0)
        skipped += int(out.get("skipped") or 0)
    print(f"ingest: строк={len(rows)} added={added} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
