#!/usr/bin/env python3
"""burn_report.py — burn-таблица по каналам (неделя-0 внутри стройки).
Запуск: python scripts/burn_report.py [дней]  (из корня репо)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import burn  # noqa: E402


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(burn.render_text(days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
