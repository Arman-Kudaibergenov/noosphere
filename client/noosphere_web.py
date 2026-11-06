"""Noosphere — нативное окно Windows (pywebview/Edge WebView2) над web-UI.

Запуск: pythonw client/noosphere_web.py
UI: client/web/index.html. JS ходит в сервер напрямую; Python отдаёт только
конфиг (сервер LAN-first + токен из ~/.secrets/noosphere.token).
"""
import json
import urllib.request
from pathlib import Path

import webview

CANDIDATES = ["http://192.168.0.235:8300", "http://100.95.239.47:8300"]
TOKEN_FILE = Path.home() / ".secrets" / "noosphere.token"
HTML = Path(__file__).parent / "web" / "index.html"


def pick_server() -> str:
    for url in CANDIDATES:
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as r:
                if json.loads(r.read().decode()).get("status") == "ok":
                    return url
        except Exception:
            continue
    return CANDIDATES[-1]


class Api:
    def get_config(self):
        token = TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.exists() else ""
        return {"server": pick_server(), "token": token}


if __name__ == "__main__":
    webview.create_window("Ноосфера", str(HTML), js_api=Api(),
                          width=1180, height=780, min_size=(900, 600))
    webview.start()
