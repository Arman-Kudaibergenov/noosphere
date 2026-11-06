"""Автозапуск при входе в Windows: значение Run в HKCU (без админ-прав).

Команда — `pythonw.exe "…\\noosphere_app.pyw" --minimized`: приложение поднимается
свёрнутым в трей и не перехватывает фокус при входе.
"""
from __future__ import annotations

import sys
import winreg
from pathlib import Path

from . import appconfig

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = appconfig.APP_ID
MINIMIZED_ARG = "--minimized"


def pythonw() -> Path:
    """Интерпретатор без консоли рядом с текущим (или первый pythonw из PATH)."""
    sibling = Path(sys.executable).with_name("pythonw.exe")
    if sibling.exists():
        return sibling
    import shutil

    found = shutil.which("pythonw")
    return Path(found) if found else Path(sys.executable)


def command(entry: Path | None = None, minimized: bool = True) -> str:
    target = entry or appconfig.ENTRY_PYW
    suffix = f" {MINIMIZED_ARG}" if minimized else ""
    return f'"{pythonw()}" "{target}"{suffix}'


def read() -> dict[str, object]:
    """Текущее значение Run: {enabled, command, matches}."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return {"enabled": False, "command": "", "matches": False}
    return {"enabled": True, "command": str(value), "matches": str(value) == command()}


def enabled() -> bool:
    return bool(read()["enabled"])


def install(entry: Path | None = None, minimized: bool = True) -> str:
    cmd = command(entry, minimized)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
    return cmd


def remove() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except OSError:
        return False


def toggle(entry: Path | None = None) -> bool:
    """Включает/выключает автозапуск; возвращает новое состояние."""
    if enabled():
        remove()
        return False
    install(entry)
    return True
