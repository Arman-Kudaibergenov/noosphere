"""Разовый прогон ночного судьи с малым капом (для проверок после деплоя).

Запуск на хосте:  ./.venv/bin/python -m scripts.audit_nightly_once [--cap 3]
Тратит реальные токены класса audit (deep) — только по явному решению владельца.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import chat_judge  # noqa: E402


def main() -> int:
    cap = 3
    if "--cap" in sys.argv:
        cap = int(sys.argv[sys.argv.index("--cap") + 1])
    summary = asyncio.run(chat_judge.nightly(cap=cap, idle_hours=6.0))
    print("NIGHTLY:", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
