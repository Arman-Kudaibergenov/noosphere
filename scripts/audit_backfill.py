"""Бэкфилл признаков правил аудита за N дней (0 LLM-токенов).

Запуск на хосте сервиса:  python -m scripts.audit_backfill --days 30
Пересчитывает turn_verdict/verdict_signals по существующим turns/tool_audit/
approvals; вердикты чатов оставит судье (ночной прогон). Боевую БД не трогает
ничем, кроме своих двух таблиц: строки кладутся с текущей rules_version.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import db, verdicts  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    db.init_schema()
    sessions = verdicts.recompute_days(a.days)
    c = db.get_db()
    rows = c.execute("SELECT verdict, COUNT(*) n FROM turn_verdict"
                     " WHERE rules_version=? GROUP BY verdict",
                     (verdicts.RULES_VERSION,)).fetchall()
    dist = ", ".join(f"{r['verdict']}={r['n']}" for r in rows) or "пусто"
    print(f"backfill: сессий={sessions} за {a.days} дн.; вердикты ходов: {dist}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
