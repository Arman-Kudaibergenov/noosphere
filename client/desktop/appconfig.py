"""Конфиг десктоп-оболочки: серверы, токен, пути, имена Win32-объектов.

Токен читается из файла и живёт только в памяти окна (правило G5 фронта): в
журнал и в диагностику попадают наличие и длина, но не значение.

Переопределения для проверок (selftest, прогон с мёртвым сервером):
  NOOSPHERE_SERVERS    список адресов через запятую вместо LAN+tailnet
  NOOSPHERE_TOKEN_FILE путь к файлу токена
  NOOSPHERE_HOME       каталог журнала и состояния (по умолчанию %LOCALAPPDATA%\\Noosphere)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

APP_TITLE = "Ноосфера"
APP_ID = "Noosphere"

# Адреса сервера по умолчанию нейтральные; боевые списки инстансов задаются
# через NOOSPHERE_SERVERS (список через запятую, порядок = приоритет).
DEFAULT_SERVERS: tuple[str, ...] = (
    "http://127.0.0.1:8300",
)

# Имена в Local\ — видны внутри сессии пользователя, чужая сессия их не достанет.
MUTEX_NAME = "Local\\NoosphereDesktop.SingleInstance"
SHOW_EVENT_NAME = "Local\\NoosphereDesktop.ShowWindow"

TRAY_UID = 0x4E4F          # 'NO'
TRAY_WINDOW_CLASS = "NoosphereTrayWindow"
TRAY_TOOLTIP = "Ноосфера"
TRAY_CALLBACK = 0x8002     # WM_APP+2: uCallbackMessage иконки (события мыши и балуна)
# Идентичность иконки по GUID, а не по (hwnd, uid): убитый процесс оставляет в трее
# мёртвую запись («призрак»), и клики уходят в никуда. С GUID новый процесс заменяет
# запись призрака, а NIM_DELETE по GUID вычищает её при старте.
TRAY_GUID_TEXT = "6f3c4e2a-9b5d-4e8f-a1c7-001122334455"


def guid_to_bytes(text: str) -> bytes:
    """GUID в байты смешанного порядка, как ждёт Windows."""
    parts = text.split("-")
    return (bytes.fromhex(parts[0])[::-1] + bytes.fromhex(parts[1])[::-1]
            + bytes.fromhex(parts[2])[::-1] + bytes.fromhex(parts[3])
            + bytes.fromhex(parts[4]))

# Команды меню трея и клика по иконке (WM_COMMAND → id).
CMD_SHOW = 1001
CMD_DRAWER_ALERTS = 1002
CMD_DRAWER_VAULT = 1010        # вкладка шторки «Доступы» прямо из трея
CMD_CHECK = 1004
CMD_AUTOSTART = 1005
CMD_BALLOON = 1006         # клик по тосту; полезная нагрузка — action уведомления
CMD_NOTIFY_TEST = 1007
CMD_UPDATE = 1008            # «Получить обновление»: git pull + перезагрузка страницы
CMD_QUIT = 1009

PROBE_TIMEOUT = 2.5        # сек на пробу /health при старте
API_TIMEOUT = 30.0         # сек на обычный запрос наблюдателя
POLL_INTERVAL = 30.0       # сек; тот же ритм, что у шторки в web-UI

CLIENT_DIR = Path(__file__).resolve().parent.parent   # .../client
REPO_ROOT = CLIENT_DIR.parent                         # .../noosphere
DESKTOP_DIR = Path(__file__).resolve().parent         # .../client/desktop
WEB_DIR = CLIENT_DIR / "web"
INDEX_HTML = WEB_DIR / "index.html"
DIAGNOSE_HTML = WEB_DIR / "diagnose.html"
ICON_PATH = DESKTOP_DIR / "noosphere.ico"
ENTRY_PYW = CLIENT_DIR / "noosphere_app.pyw"


def servers() -> tuple[str, ...]:
    env = os.environ.get("NOOSPHERE_SERVERS", "").strip()
    if env:
        return tuple(part.strip().rstrip("/") for part in env.split(",") if part.strip())
    saved = _saved_servers()
    if saved:
        return saved
    return DEFAULT_SERVERS


def client_cfg_path() -> Path:
    return home_dir() / "client.json"


def _saved_servers() -> tuple[str, ...]:
    """Список серверов, сохранённый при первом запуске (диалог «адрес сервера»)."""
    try:
        data = json.loads(client_cfg_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    items = [str(u).strip().rstrip("/") for u in data.get("servers") or []]
    return tuple(u for u in items if u.startswith("http"))


def save_servers(urls: list[str]) -> None:
    """Запоминает адрес(а) сервера инстанса — чтобы следующий запуск был без вопросов."""
    client_cfg_path().write_text(
        json.dumps({"servers": [u.strip().rstrip("/") for u in urls if u.strip()]},
                   ensure_ascii=False, indent=2), encoding="utf-8")


def token_file() -> Path:
    env = os.environ.get("NOOSPHERE_TOKEN_FILE", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".secrets" / "noosphere.token"


def read_token() -> str:
    """Токен из файла; пустая строка, если файла нет или он пуст."""
    path = token_file()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def home_dir() -> Path:
    """Каталог журнала и состояния приложения."""
    env = os.environ.get("NOOSPHERE_HOME", "").strip()
    if env:
        base = Path(env)
    else:
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        base = Path(local) / APP_ID
    base.mkdir(parents=True, exist_ok=True)
    return base


def log_path() -> Path:
    return home_dir() / "app.log"


def seen_path() -> Path:
    return home_dir() / "seen.json"
