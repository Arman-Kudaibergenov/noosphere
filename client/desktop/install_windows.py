"""Установка «приложения» без сборки: ярлыки Desktop/Пуск + автозапуск.

Ярлык = `pythonw.exe` + `noosphere_app.pyw` + наша иконка; автозапуск = значение
`Noosphere` в `HKCU\\...\\Run` с флагом `--minimized` (поднимается в трей и не
крадёт фокус при входе). Всё обратимо: `--uninstall` убирает ярлыки и ключ,
журнал и состояние не трогает.

Пути Desktop/Пуск берём через SHGetKnownFolderPath, а не из %USERPROFILE%:
у владельца Desktop может быть перенаправлен в OneDrive.

Использование:
  python client\\desktop\\install_windows.py            установить
  python client\\desktop\\install_windows.py --status   показать состояние
  python client\\desktop\\install_windows.py --uninstall убрать ярлыки и автозапуск
"""
from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
from pathlib import Path

try:
    from . import appconfig, applog, autostart, make_icon
except ImportError:    # запуск как скрипт: python client\desktop\install_windows.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from desktop import appconfig, applog, autostart, make_icon

FOLDERID_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"
FOLDERID_PROGRAMS = "A77F5D77-2E2B-44C3-A6A2-ABA601054A51"
SHORTCUT_NAME = appconfig.APP_ID + ".lnk"


def _guid_bytes(text: str) -> bytes:
    """GUID в байты смешанного порядка, как ждёт Windows."""
    parts = text.split("-")
    return (bytes.fromhex(parts[0])[::-1] + bytes.fromhex(parts[1])[::-1]
            + bytes.fromhex(parts[2])[::-1] + bytes.fromhex(parts[3])
            + bytes.fromhex(parts[4]))


def known_folder(guid: str) -> Path | None:
    """Известная папка оболочки (Desktop, Пуск) для текущего пользователя."""
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                             ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_wchar_p)]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    raw = ctypes.c_wchar_p()
    handle = (ctypes.c_byte * 16).from_buffer_copy(_guid_bytes(guid))
    code = shell32.SHGetKnownFolderPath(ctypes.byref(handle), 0, None, ctypes.byref(raw))
    try:
        if code != 0 or not raw.value:
            return None
        return Path(raw.value)
    finally:
        ole32.CoTaskMemFree(raw)


def shortcut_paths() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    desktop = known_folder(FOLDERID_DESKTOP) or Path.home() / "Desktop"
    programs = known_folder(FOLDERID_PROGRAMS) or (
        Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    out.append(("desktop", desktop / SHORTCUT_NAME))
    out.append(("startmenu", programs / SHORTCUT_NAME))
    return out


def _ps_quote(text: str) -> str:
    return text.replace("'", "''")


def make_shortcut(path: Path, minimized: bool = False) -> bool:
    """Пишет .lnk через WScript.Shell (PowerShell, без админ-прав).

    Ярлык стартует окном, а не трейем: «нормальное приложение» открывается лицом;
    в трей уходит только автозапуск при входе (autostart.command(minimized=True)).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    icon = appconfig.ICON_PATH
    if not icon.exists():
        make_icon.build(icon)
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('"
        + _ps_quote(str(path)) + "');"
          "$s.TargetPath='" + _ps_quote(str(autostart.pythonw())) + "';"
          "$s.Arguments='\"" + _ps_quote(str(appconfig.ENTRY_PYW)) + "\""
          + (" --minimized" if minimized else "") + "';"
          "$s.WorkingDirectory='" + _ps_quote(str(appconfig.REPO_ROOT)) + "';"
          "$s.IconLocation='" + _ps_quote(str(icon)) + ",0';"
          "$s.Description='Ноосфера — личное рабочее место ИИ-агентов';"
          "$s.Save()"
    )
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-Command", script],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        applog.error("shortcut failed", path=str(path), error=str(exc))
        return False
    if proc.returncode != 0:
        applog.error("shortcut failed", path=str(path),
                     stderr=proc.stderr.strip()[:300])
        return False
    return path.exists()


def install(autostart_on: bool = True) -> dict[str, object]:
    """Ставит ярлыки и автозапуск; возвращает сводку для журнала и отчёта."""
    result: dict[str, object] = {"shortcuts": {}, "autostart": None}
    for name, path in shortcut_paths():
        ok = make_shortcut(path)
        result["shortcuts"][name] = {"path": str(path), "ok": ok}
        applog.info("install shortcut", where=name, path=str(path), ok=ok)
    if autostart_on:
        cmd = autostart.install()
        result["autostart"] = {"enabled": True, "command": cmd}
        applog.info("install autostart", command=cmd)
    else:
        result["autostart"] = autostart.read()
    return result


def uninstall() -> dict[str, object]:
    result: dict[str, object] = {"shortcuts": {}, "autostart_removed": False}
    for name, path in shortcut_paths():
        try:
            path.unlink(missing_ok=True)
            result["shortcuts"][name] = {"path": str(path), "removed": not path.exists()}
        except OSError as exc:
            result["shortcuts"][name] = {"path": str(path), "removed": False,
                                         "error": str(exc)}
    result["autostart_removed"] = autostart.remove()
    applog.info("uninstall done", **{k: str(v) for k, v in result.items()})
    return result


def status() -> dict[str, object]:
    return {
        "shortcuts": [{"where": name, "path": str(path), "exists": path.exists()}
                      for name, path in shortcut_paths()],
        "autostart": autostart.read(),
        "icon": {"path": str(appconfig.ICON_PATH), "exists": appconfig.ICON_PATH.exists()},
        "entry": {"path": str(appconfig.ENTRY_PYW), "exists": appconfig.ENTRY_PYW.exists()},
        "pythonw": str(autostart.pythonw()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true", help="убрать ярлыки и автозапуск")
    parser.add_argument("--no-autostart", action="store_true", help="не трогать ключ Run")
    parser.add_argument("--status", action="store_true", help="показать состояние установки")
    args = parser.parse_args(argv)

    if args.status:
        data = status()
        for row in data["shortcuts"]:
            print(f"STATUS shortcut {row['where']}: exists={row['exists']} {row['path']}")
        auto = data["autostart"]
        print(f"STATUS autostart: enabled={auto['enabled']} matches={auto['matches']}")
        print(f"STATUS pythonw: {data['pythonw']}")
        return 0

    if args.uninstall:
        data = uninstall()
        for name, row in data["shortcuts"].items():
            print(f"UNINSTALL shortcut {name}: removed={row.get('removed')} {row['path']}")
        print(f"UNINSTALL autostart: removed={data['autostart_removed']}")
        return 0

    data = install(autostart_on=not args.no_autostart)
    for name, row in data["shortcuts"].items():
        print(f"INSTALL shortcut {name}: ok={row['ok']} {row['path']}")
    auto = data["autostart"]
    print(f"INSTALL autostart: enabled={auto.get('enabled')} command={auto.get('command', '')}")
    return 0 if all(r["ok"] for r in data["shortcuts"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
