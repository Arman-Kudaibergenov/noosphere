#!/usr/bin/env python3
"""quota_probe.py — read-only пробы квот всех enabled-каналов (неделя-0 внутри стройки).
Запуск: python scripts/quota_probe.py  (из корня репо)"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import quotas  # noqa: E402


def main():
    readings = quotas.probe_all()
    for r in readings:
        rem = r.get("remaining")
        rem_s = f"{rem} {r.get('unit') or ''}" if rem is not None else "—"
        print(f"{r['channel']:10s} ok={int(r['ok'])} остаток: {rem_s:12s} "
              f"окно: {r.get('window') or '—'}")
    print(json.dumps(readings, ensure_ascii=False, indent=1) if "--json" in sys.argv else "", end="")
    return 0 if any(r["ok"] for r in readings) else 1


if __name__ == "__main__":
    sys.exit(main())
