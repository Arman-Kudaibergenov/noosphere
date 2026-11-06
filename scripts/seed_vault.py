#!/usr/bin/env python3
"""seed_vault.py — первичное наполнение vault картой контура (идемпотентно).

Записи НЕ живут в репозитории: карта доступов инстанса — это его данные,
а не код. Скрипт читает JSON-массив записей из файла:
  1. env VAULT_SEED_FILE, иначе
  2. <repo>/data/vault_seed.json (data/ в .gitignore — локальные данные инстанса).

Формат записи: {"kind","name","host"?,"login"?,"secret"?,"secret_file"?,
"notes"?,"tags"?}. Значений секретов в файле быть не должно — только
указатели (secret_file) на ~/.secrets.

Запуск: ./.venv/bin/python scripts/seed_vault.py; повторный запуск обновляет
записи (upsert kind+name). Пример контура заказчика — scripts/seed_vault_example.json.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import vault  # noqa: E402


def _seed_path() -> Path:
    env = os.environ.get("VAULT_SEED_FILE", "").strip()
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent / "data" / "vault_seed.json"


def main() -> int:
    p = _seed_path()
    if not p.exists():
        print(f"файл сидов не найден: {p}\n"
              "положи JSON-массив записей (см. scripts/seed_vault_example.json) "
              "или задай VAULT_SEED_FILE")
        return 1
    entries = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        print(f"{p}: ожидается JSON-массив записей")
        return 1
    for e in entries:
        entry_id = vault.upsert(e["kind"], e["name"], host=e.get("host", ""),
                                login=e.get("login", ""), secret=e.get("secret", ""),
                                secret_file=e.get("secret_file", ""),
                                notes=e.get("notes", ""), tags=e.get("tags", ""))
        print(f"ok {e['kind']}/{e['name']} id={entry_id}")
    print(f"всего записей: {len(entries)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
