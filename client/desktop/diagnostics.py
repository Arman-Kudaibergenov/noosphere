"""Экран диагностики: что проверено, что сломано и что делать.

Решение принимается до создания окна: живой сервер + токен → web-UI, иначе →
`web/diagnose.html`. Белого окна с непонятной причиной быть не должно — это
отдельный пункт заказа владельца от 2026-10-11.
"""
from __future__ import annotations

import sys
import winreg
from dataclasses import dataclass
from pathlib import Path

from . import appconfig, applog, httpapi
from .autostart import read as autostart_read

WEBVIEW2_CLIENT = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_CLIENT_ALT = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""
    kind: str = "other"
    ms: int = 0


def webview2_version() -> str:
    """Версия WebView2 Runtime из реестра; пустая строка, если не найден."""
    for root, path in ((winreg.HKEY_LOCAL_MACHINE, WEBVIEW2_CLIENT),
                       (winreg.HKEY_LOCAL_MACHINE, WEBVIEW2_CLIENT_ALT),
                       (winreg.HKEY_CURRENT_USER, WEBVIEW2_CLIENT_ALT)):
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, "pv")
                if value and str(value) != "0.0.0.0":
                    return str(value)
        except OSError:
            continue
    return ""


def run_checks(timeout: float = appconfig.PROBE_TIMEOUT) -> tuple[list[Check], httpapi.Probe | None]:
    """Прогоняет проверки и возвращает их вместе с выбранным живым сервером."""
    checks: list[Check] = []
    probes = httpapi.probe_all(timeout=timeout)
    for item in probes:
        checks.append(Check(
            name=f"Сервер · {item.label}",
            ok=item.ok,
            detail=(f"{item.url} · {item.ms} мс"
                    + (f" · версия {item.version}" if item.ok else f" · {item.error}")),
            hint="" if item.ok else "Сервер на Mac mini не отвечает: проверь, что Мак жив и uvicorn на :8300 запущен",
            kind="server", ms=item.ms))
    alive = httpapi.pick_server(probes)

    token = appconfig.read_token()
    token_path = appconfig.token_file()
    checks.append(Check(
        name="Токен доступа",
        ok=bool(token),
        detail=(f"{token_path} · {len(token)} зн." if token else f"{token_path} · файла нет или он пуст"),
        hint="" if token else "Скопируй токен сервера в этот файл одной строкой (без кавычек)",
        kind="token"))

    wv2 = webview2_version()
    checks.append(Check(
        name="WebView2 Runtime",
        ok=bool(wv2),
        detail=(f"версия {wv2}" if wv2 else "не найден в реестре"),
        hint="" if wv2 else "Нужен Microsoft Edge WebView2 Runtime — обычно входит в Windows 11",
        kind="runtime"))

    missing = [p.name for p in (appconfig.INDEX_HTML, appconfig.DIAGNOSE_HTML) if not p.exists()]
    checks.append(Check(
        name="Файлы клиента",
        ok=not missing,
        detail=(f"{appconfig.WEB_DIR} · index.html и diagnose.html на месте"
                if not missing else f"нет файлов: {', '.join(missing)}"),
        hint="" if not missing else "Клиент повреждён: восстанови рабочую копию noosphere",
        kind="files"))

    pyw = Path(sys.executable).with_name("pythonw.exe")
    checks.append(Check(
        name="Интерпретатор pythonw",
        ok=pyw.exists(),
        detail=str(pyw) if pyw.exists() else f"рядом с {sys.executable} нет pythonw.exe",
        hint="" if pyw.exists() else "Ярлык будет запускать python.exe — мелькнёт консоль",
        kind="runtime"))

    return checks, alive


def decide(checks: list[Check], alive: httpapi.Probe | None) -> tuple[str, str]:
    """("ui", server) — показываем рабочее место; ("diagnose", причина) — экран диагностики."""
    if alive is None:
        return "diagnose", "нет живого сервера"
    token_ok = next((c.ok for c in checks if c.kind == "token"), False)
    if not token_ok:
        return "diagnose", "нет токена"
    files_ok = next((c.ok for c in checks if c.kind == "files"), False)
    if not files_ok:
        return "diagnose", "нет файлов клиента"
    return "ui", alive.url


def rows(checks: list[Check]) -> list[dict[str, object]]:
    """Проверки в виде, пригодном для JSON в страницу (без секретов)."""
    return [{"name": c.name, "ok": c.ok, "detail": c.detail, "hint": c.hint,
             "kind": c.kind, "ms": c.ms} for c in checks]


def payload(checks: list[Check], decision: str, server: str, reason: str) -> dict[str, object]:
    """Полный снимок для `window.pywebview.api.get_diag()`."""
    return {
        "decision": decision,
        "reason": reason,
        "server": server,
        "servers": list(appconfig.servers()),
        "rows": rows(checks),
        "token_file": str(appconfig.token_file()),
        "token_present": bool(appconfig.read_token()),
        "home": str(appconfig.home_dir()),
        "log_path": str(appconfig.log_path()),
        "repo": str(appconfig.REPO_ROOT),
        "python": sys.version.split()[0],
        "autostart": autostart_read(),
        "log_tail": applog.tail(25),
    }
