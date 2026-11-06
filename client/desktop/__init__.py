"""Десктоп-оболочка «Ноосферы» для Windows.

Ноутбук — только окно: сервер, инструменты и секреты остаются на Mac mini :8300
(решение владельца 2026-10-11). Оболочка добавляет то, чего не даёт страница в
pywebview: один экземпляр, трей, системные уведомления о crit-алертах,
автозапуск и экран диагностики вместо белого окна.

Состав (stdlib-only, без сторонних пакетов — под Python 3.14 колёс может не быть):
  appconfig      адреса сервера, токен, пути, имена Win32-объектов
  applog         %LOCALAPPDATA%\\Noosphere\\app.log с ротацией
  httpapi        GET/POST к серверу + проба /health
  diagnostics    проверки связи/токена/WebView2 и решение «UI или диагностика»
  single_instance именованный mutex + событие «покажи окно»
  notify         событие → полезная нагрузка тоста, кап от шторма
  watcher        опрос /v1/alerts, диффы по id
  tray           Shell_NotifyIconW: иконка, меню, балун-тост (свой поток)
  make_icon      генератор noosphere.ico (zlib+struct, без Pillow)
  install_windows ярлыки Desktop/Пуск + автозапуск HKCU\\...\\Run
  selftest       headless-проверки без GUI: `python client\\desktop\\selftest.py`

Вход приложения — `client\\noosphere_app.pyw`.
"""

__all__ = [
    "appconfig", "applog", "httpapi", "diagnostics", "single_instance",
    "notify", "watcher", "tray",
]
