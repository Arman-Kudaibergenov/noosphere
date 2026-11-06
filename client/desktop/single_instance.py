"""Один экземпляр приложения: именованный mutex + событие «покажи окно».

Второй запуск не плодит окно: он взводит событие и сразу выходит, а первый
экземпляр (поток-наблюдатель) разворачивает и фокусирует уже открытое окно.
Имена в пространстве `Local\\` — в пределах сессии пользователя.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

from . import appconfig

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.SetEvent.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class SingleInstance:
    """Держит mutex и событие; `acquire()` отвечает, мы ли первые."""

    def __init__(self, mutex_name: str = appconfig.MUTEX_NAME,
                 event_name: str = appconfig.SHOW_EVENT_NAME) -> None:
        self.mutex_name = mutex_name
        self.event_name = event_name
        self._mutex: int | None = None
        self._event: int | None = None
        self.first: bool = False

    def acquire(self, wait_s: float = 0.0) -> bool:
        """True — экземпляр первый (mutex создан нами), False — уже запущен.

        wait_s > 0 — ждать освобождения mutex (рукопожатие при перезапуске
        оболочки: новая копия стартует раньше, чем старая успела выйти).
        """
        deadline = time.monotonic() + wait_s
        while True:
            handle = kernel32.CreateMutexW(None, False, self.mutex_name)
            if not handle:
                # Не смогли создать mutex — считаем себя первыми, приложение важнее.
                self.first = True
                return True
            self._mutex = int(handle)
            self.first = ctypes.get_last_error() != ERROR_ALREADY_EXISTS
            if self.first or time.monotonic() >= deadline:
                return self.first
            kernel32.CloseHandle(self._mutex)
            self._mutex = None
            time.sleep(0.25)

    def _open_event(self) -> int | None:
        if self._event is None:
            # auto-reset, изначально не взведено
            handle = kernel32.CreateEventW(None, False, False, self.event_name)
            self._event = int(handle) if handle else None
        return self._event

    def request_show(self) -> bool:
        """Второй экземпляр: просит первый показать окно."""
        event = self._open_event()
        return bool(event and kernel32.SetEvent(event))

    def watch_show(self, on_show, stop: threading.Event | None = None,
                   wait_ms: int = 400) -> None:
        """Блокирующий цикл первого экземпляра: ждёт сигнал и зовёт `on_show`."""
        event = self._open_event()
        if not event:
            return
        while not (stop and stop.is_set()):
            state = kernel32.WaitForSingleObject(event, wait_ms)
            if state == WAIT_OBJECT_0:
                try:
                    on_show()
                except Exception:
                    pass  # сбой показа окна не должен ронять наблюдателя
            elif state != WAIT_TIMEOUT:
                return  # ошибка ожидания — выходим, цикл бессмыслен

    def release(self) -> None:
        for attr in ("_event", "_mutex"):
            handle = getattr(self, attr)
            if handle:
                kernel32.CloseHandle(handle)
                setattr(self, attr, None)
