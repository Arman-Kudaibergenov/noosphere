"""Трей-иконка и тосты на Win32 (ctypes), без сторонних пакетов.

Почему своё: pywebview 6.2.1 трей не умеет, pystray/winotify тянут Pillow, а под
Python 3.14 колёс может не быть. `Shell_NotifyIconW` + балун = штатное уведомление
Windows 10/11, ноль зависимостей.

Поток: HWND трея создаётся в своём потоке со своим оконным циклом, потому что
основной поток занят pywebview (STA/WinForms). Из чужих потоков сюда приходит
только `PostMessage`; сам `Shell_NotifyIconW` вызывается на потоке-владельце окна,
как требует документация. `send()` блокируется до получения кода возврата —
наблюдателю нужен честный rc, чтобы писать `toast Shell_NotifyIconW=1`.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from dataclasses import dataclass

from . import appconfig, applog

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# Свои алиасы: указательного размера на 32 и 64 битах, без догадок про wintypes.
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_QUIT = 0x0012
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_USER = 0x0400
WM_APP = 0x8000
NIN_BALLOONUSERCLICK = WM_USER + 5
NIN_BALLOONTIMEOUT = WM_USER + 4
NIN_BALLOONHIDE = WM_USER + 3

WM_APP_NOTIFY = WM_APP + 1      # наше: «в очереди есть тост, отправь»
WM_APP_TOOLTIP = WM_APP + 3     # наше: «обнови подпись иконки»
                                # WM_APP + 2 = appconfig.TRAY_CALLBACK (uCallbackMessage)

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIF_GUID = 0x20
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x1, 0x2, 0x3

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_GRAYED = 0x0001
MF_CHECKED = 0x0008
MF_UNCHECKED = 0x0000
TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_BOTTOMALIGN = 0x0020

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
IDI_APPLICATION = 32512
IDC_ARROW = 32512


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", WPARAM), ("lParam", LPARAM), ("time", wintypes.DWORD),
                ("pt", POINT), ("lPrivate", wintypes.DWORD)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", wintypes.INT), ("cbWndExtra", wintypes.INT),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", ctypes.c_wchar * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", ctypes.c_wchar * 256),
                ("uTimeout", wintypes.UINT), ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON)]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = ctypes.c_ushort
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, wintypes.INT, wintypes.INT, wintypes.INT,
                                   wintypes.INT, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wintypes.HWND
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.INT
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, WPARAM, LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.PostQuitMessage.argtypes = [wintypes.INT]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, WPARAM, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.INT, wintypes.INT,
                                  wintypes.INT, wintypes.HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = ctypes.c_void_p
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = wintypes.HICON
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              wintypes.INT, wintypes.INT, wintypes.UINT]
user32.LoadImageW.restype = ctypes.c_void_p
user32.DestroyWindow.argtypes = [wintypes.HWND]
shell32.Shell_NotifyIconW.argtypes = [wintypes.UINT, ctypes.c_void_p]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


@dataclass
class MenuItem:
    id: int
    text: str = ""
    checked: bool = False
    enabled: bool = True
    separator: bool = False


def load_icon(path):
    """HICON из .ico; при любой неудаче — стандартная иконка приложения."""
    if path:
        try:
            handle = user32.LoadImageW(None, str(path), IMAGE_ICON, 0, 0,
                                       LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if handle:
                return ctypes.c_void_p(handle)
        except OSError:
            pass
    return user32.LoadIconW(None, IDI_APPLICATION)


def set_app_user_model_id(aumid: str = "Arman.Noosphere.Desktop") -> int:
    """Идентификатор приложения для тостов (иначе Windows пишет «Проводник»)."""
    try:
        return int(shell32.SetCurrentProcessExplicitAppUserModelID(aumid))
    except (AttributeError, OSError):
        return -1


class Tray:
    """Иконка в трее: меню, показ окна по клику, балун-тосты."""

    def __init__(self, on_command, menu_provider, icon_path=None,
                 tooltip: str = appconfig.TRAY_TOOLTIP) -> None:
        self._on_command = on_command
        self._menu_provider = menu_provider
        self._icon_path = icon_path
        self._tooltip = tooltip
        self._hwnd: int | None = None
        self._hicon = None
        self._nid = None
        self._thread: threading.Thread | None = None
        self._tid: int = 0
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._outbox: list[tuple[dict, dict]] = []
        self._last_action = ""
        self._msg_count = 0
        # ссылка на колбэк обязательна: иначе GC уничтожит его между вызовами
        self._wndproc_ref = WNDPROC(self._wndproc)

    # ── жизненный цикл ──
    def start(self, timeout: float = 5.0) -> bool:
        """Поднимает поток трея и ждёт, пока иконка добавлена."""
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self._main, name="noosphere-tray",
                                        daemon=True)
        self._thread.start()
        ok = self._ready.wait(timeout)
        applog.info("tray start", ok=ok, hwnd=bool(self._hwnd))
        return ok and bool(self._hwnd)

    def stop(self) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
        if self._tid:
            kernel32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=3.0)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._hwnd)

    def set_tooltip(self, text: str) -> None:
        with self._lock:
            self._tooltip = text[:127]
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_APP_TOOLTIP, 0, 0)

    # ── тосты ──
    def send(self, payload: dict) -> int:
        """Отправляет балун и возвращает код Shell_NotifyIconW (1 = успех)."""
        if not self._hwnd:
            applog.warn("toast skipped: no tray window", kind=payload.get("kind", ""))
            return 0
        box: dict = {"rc": 0, "done": threading.Event()}
        with self._lock:
            self._outbox.append((payload, box))
        user32.PostMessageW(self._hwnd, WM_APP_NOTIFY, 0, 0)
        box["done"].wait(3.0)
        return int(box["rc"])

    def _flush_outbox(self) -> None:
        while True:
            with self._lock:
                if not self._outbox:
                    return
                payload, box = self._outbox.pop(0)
            rc = self._balloon(payload)
            box["rc"] = rc
            box["done"].set()

    def _balloon(self, payload: dict) -> int:
        nid = self._base_nid()
        # NIF_GUID обязателен в modify: без него балун на GUID-иконке молча отклоняется
        nid.uFlags = NIF_INFO | NIF_GUID
        nid.szInfo = str(payload.get("text", ""))[:255]
        nid.szInfoTitle = str(payload.get("title", ""))[:63]
        nid.dwInfoFlags = int(payload.get("flags", NIIF_INFO))
        nid.uTimeout = 0
        self._last_action = str(payload.get("action", ""))
        rc = int(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))
        # строка журнала читается как «toast Shell_NotifyIconW=1» — по ней видно,
        # что балун действительно принят оболочкой Windows
        applog.info(f"toast Shell_NotifyIconW={rc}", kind=payload.get("kind", ""),
                    title=payload.get("title", ""))
        return rc

    # ── Win32 ──
    def _base_nid(self) -> NOTIFYICONDATAW:
        if self._nid is None:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = appconfig.TRAY_UID
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_GUID
            nid.uCallbackMessage = appconfig.TRAY_CALLBACK
            nid.hIcon = self._hicon
            nid.szTip = self._tooltip[:127]
            nid.guidItem = (ctypes.c_byte * 16).from_buffer_copy(
                appconfig.guid_to_bytes(appconfig.TRAY_GUID_TEXT))
            self._nid = nid
        else:
            nid = self._nid
            nid.hIcon = self._hicon
        return nid

    def _main(self) -> None:
        self._tid = int(kernel32.GetCurrentThreadId())
        hinst = kernel32.GetModuleHandleW(None)
        self._hicon = load_icon(self._icon_path)

        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, IDC_ARROW)
        wc.lpszClassName = appconfig.TRAY_WINDOW_CLASS
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err != 1410:            # ERROR_CLASS_ALREADY_EXISTS — класс уже наш
                applog.error("tray RegisterClassW failed", error=err)
                self._ready.set()
                return

        self._hwnd = user32.CreateWindowExW(
            0, appconfig.TRAY_WINDOW_CLASS, "Ноосфера", 0, 0, 0, 0, 0,
            None, None, hinst, None)
        if not self._hwnd:
            applog.error("tray CreateWindowExW failed", error=ctypes.get_last_error())
            self._ready.set()
            return

        # вычистить призрак прошлого процесса: идентичность записи теперь по GUID,
        # поэтому DELETE по нашему GUID убирает мёртвую запись чужого hwnd
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._base_nid()))
        rc = int(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._base_nid())))
        applog.info("tray icon added", rc=rc, tooltip=self._tooltip, guid=True)
        self._ready.set()

        msg = MSG()
        while True:
            state = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if state == 0 or state == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._base_nid()))
        applog.info("tray loop exited")

    def _wndproc(self, hwnd, message, wparam, lparam):
        # первые сообщения пишем в журнал: смерть потока трея иначе невидима
        # (pythonw не показывает stderr), а симптом — «иконка есть, клики мертвы»
        self._msg_count += 1
        if self._msg_count <= 25:
            applog.info("tray msg", message=hex(int(message)), lparam=hex(int(lparam)))
        try:
            return self._wndproc_body(hwnd, message, wparam, lparam)
        except Exception as exc:
            import traceback

            applog.error("tray wndproc crashed", message=hex(int(message)),
                         error=f"{type(exc).__name__}: {exc}",
                         tb=traceback.format_exc(limit=6).replace("\n", " | "))
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _wndproc_body(self, hwnd, message, wparam, lparam):
        if message == WM_APP_NOTIFY:
            self._flush_outbox()
            return 0
        if message == WM_APP_TOOLTIP:
            nid = self._base_nid()
            nid.uFlags = NIF_TIP
            with self._lock:
                nid.szTip = self._tooltip[:127]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            return 0
        if message == WM_COMMAND:
            self._fire(int(wparam & 0xFFFF))
            return 0
        if message == appconfig.TRAY_CALLBACK:
            # lparam = событие мыши/блуна на самой иконке трея
            if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._fire(appconfig.CMD_SHOW)
            elif lparam == WM_RBUTTONUP:
                self._popup_menu()
            elif lparam == NIN_BALLOONUSERCLICK:
                self._fire(appconfig.CMD_BALLOON, self._last_action)
            return 0
        if message == WM_CONTEXTMENU:
            # часть оболочек шлёт его вместо WM_RBUTTONUP в колбэке (клавиатура, тач)
            self._popup_menu()
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _fire(self, command: int, payload: str = "") -> None:
        try:
            self._on_command(command, payload)
        except Exception as exc:
            applog.error("tray command failed", command=command,
                         error=f"{type(exc).__name__}: {exc}")

    def _popup_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        if not menu:
            applog.error("tray menu CreatePopupMenu failed", error=ctypes.get_last_error())
            return
        applog.info("tray menu opened")
        try:
            for item in self._menu_provider() or []:
                if item.separator:
                    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    continue
                flags = MF_STRING
                flags |= MF_CHECKED if item.checked else MF_UNCHECKED
                if not item.enabled:
                    flags |= MF_GRAYED
                user32.AppendMenuW(menu, flags, item.id, item.text)
            point = POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self._hwnd)
            user32.TrackPopupMenu(menu, TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
                                  point.x, point.y, 0, self._hwnd, None)
            user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)
