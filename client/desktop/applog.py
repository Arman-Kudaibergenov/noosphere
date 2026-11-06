"""Журнал десктоп-оболочки: %LOCALAPPDATA%\\Noosphere\\app.log, ротация на 1 МБ.

Формат строки — `2026-10-11T19:42:11Z INFO msg key=value`: его же читают
selftest, экран диагностики и отчёт владельцу. pythonw не показывает stderr,
поэтому журнал — единственный канал обратной связи приложения.

Значения ключей token/secret/authorization заменяются на `***` — защита от
случайной записи секрета в файл, который потом уедет в баг-репорт.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from . import appconfig

MAX_BYTES = 1_000_000
REDACT_KEYS = {"token", "secret", "authorization", "password"}

_lock = threading.Lock()


def path() -> Path:
    return appconfig.log_path()


def _value(key: str, val: object) -> str:
    if key.lower() in REDACT_KEYS:
        return "***"
    text = str(val)
    return f'"{text}"' if any(ch in text for ch in ' "\t\n') else text


def log(msg: str, level: str = "INFO", **kv: object) -> str:
    """Пишет строку в журнал и возвращает её (для проверок)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tail = "".join(f" {k}={_value(k, v)}" for k, v in kv.items())
    line = f"{ts} {level} {msg}{tail}"
    with _lock:
        try:
            target = path()
            if target.exists() and target.stat().st_size > MAX_BYTES:
                backup = target.with_name(target.stem + ".log.1")
                if backup.exists():
                    backup.unlink()
                target.replace(backup)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass  # журнал не имеет права ронять приложение
    return line


def info(msg: str, **kv: object) -> str:
    return log(msg, "INFO", **kv)


def warn(msg: str, **kv: object) -> str:
    return log(msg, "WARN", **kv)


def error(msg: str, **kv: object) -> str:
    return log(msg, "ERROR", **kv)


def tail(n: int = 60) -> list[str]:
    """Последние n строк журнала (для экрана диагностики)."""
    try:
        lines = path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-n:]
