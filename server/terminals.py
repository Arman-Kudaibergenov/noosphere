"""terminals.py — постоянные PTY-терминалы агента (FOUNDING §16.3 батч 3).

shell_exec из tools.py безстейтен: каждая команда — свежий процесс, cd и export
не переживают вызов. Терминал — живой bash: cwd и env сохраняются между командами,
на macOS /usr/bin/script даёт PTY для интерактивных программ. Долгая команда
остаётся жить после таймаута: ввод, новый вывод и закрытие — отдельные операции.
Процессы живут внутри процесса сервера и умирают вместе с ним.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time

_SHELL = shutil.which("bash") or shutil.which("sh")
_SCRIPT = shutil.which("script") if sys.platform == "darwin" else None


class Terminal:
    def __init__(self) -> None:
        self.proc: subprocess.Popen[bytes] | None = None
        self.token = secrets.token_hex(4)
        self.lock = asyncio.Lock()
        self._buf = bytearray()
        self._active = False

    async def _ensure(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        if _SHELL is None:
            raise RuntimeError("bash/sh не найден на хосте")
        argv = [_SCRIPT, "-q", "/dev/null", _SHELL] if _SCRIPT else [_SHELL]
        kwargs = {"start_new_session": True} if os.name == "posix" else {}
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, **kwargs)
        assert self.proc.stdout
        os.set_blocking(self.proc.stdout.fileno(), False)
        self._buf.clear()
        self._active = False
        ready = f"__NS_READY_{self.token}__"
        ready_line = re.compile(
            rb"(?:^|\r?\n)" + re.escape(ready.encode()) + rb"\r?\n")
        assert self.proc.stdin
        self.proc.stdin.write(f"PS1=; PS2=; stty -echo 2>/dev/null; echo {ready}\n".encode())
        deadline = time.monotonic() + 5
        while not ready_line.search(self._buf):
            if not await self._read_chunk(max(0.05, deadline - time.monotonic())):
                await self._kill()
                raise RuntimeError("не удалось запустить терминал")
            if time.monotonic() >= deadline:
                await self._kill()
                raise RuntimeError("терминал не ответил при запуске")
        self._buf.clear()

    async def _kill(self) -> None:
        proc = self.proc
        self.proc = None
        self._active = False
        self._buf.clear()
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.to_thread(proc.wait, 2)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.to_thread(proc.wait)
            except (RuntimeError, ChildProcessError):
                pass

    async def _read_chunk(self, timeout: float) -> bool:
        if self.proc is None or self.proc.stdout is None:
            return False
        deadline = time.monotonic() + timeout
        while True:
            try:
                raw = os.read(self.proc.stdout.fileno(), 65536)
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(0.05, remaining))
                continue
            except OSError:
                raw = b""
            if not raw:
                proc = self.proc
                self.proc = None
                self._active = False
                if proc is not None:
                    await asyncio.to_thread(proc.wait)
                return False
            self._buf.extend(raw)
            if len(self._buf) > 1_000_000:
                del self._buf[:-500_000]
            return True

    def _take_text(self) -> str:
        text = self._buf.decode("utf-8", errors="replace")
        self._buf.clear()
        return text

    def _completed(self) -> tuple[int, str] | None:
        if not self._active:
            return None
        marker = f"__NS_{self.token}_".encode()
        match = re.search(re.escape(marker) + rb"(\d+)__", self._buf)
        if match is None:
            return None
        rc = int(match.group(1))
        text = self._buf[:match.start()].decode("utf-8", errors="replace")
        self._buf.clear()
        self._active = False
        return rc, text

    @staticmethod
    def _format(rc: int | None, text: str, running: bool) -> str:
        if running:
            return ("[процесс продолжается: input для ввода, poll для вывода, close для остановки]\n"
                    + (text or "(пустой вывод)"))
        return (f"rc={rc}\n" if rc else "") + (text or "(пустой вывод)")

    async def _collect(self, timeout: int) -> str:
        deadline = time.monotonic() + timeout
        while True:
            completed = self._completed()
            if completed is not None:
                rc, text = completed
                return self._format(rc, text, False)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._format(None, self._take_text(), self._active)
            if not await self._read_chunk(remaining):
                if self.proc is None:
                    self._active = False
                    return (self._take_text()
                            + "\n[терминал закрыт, следующая команда — в новом]")

    async def exec(self, command: str, timeout: int) -> str:
        """Запустить команду; при таймауте оставить её жить для input/poll/close."""
        async with self.lock:
            await self._ensure()
            if self._active:
                return "отказ: прежняя команда ещё выполняется — используй input/poll/close"
            assert self.proc is not None and self.proc.stdin
            self._buf.clear()
            marker = f"__NS_{self.token}_"
            self._active = True
            payload = (f"{{\n{command}\n__NS_RC=$?\n"
                       f"echo {marker}${{__NS_RC}}__\n}}\n")
            self.proc.stdin.write(payload.encode())
            return await self._collect(timeout)

    async def input(self, data: str, enter: bool, timeout: int) -> str:
        """Передать stdin активной интерактивной команде и собрать новый вывод."""
        async with self.lock:
            if self.proc is None or not self._active:
                return "отказ: нет активной команды для ввода — сначала запусти command"
            assert self.proc.stdin
            self.proc.stdin.write(data.encode() + (b"\r" if enter else b""))
            return await self._collect(timeout)

    async def poll(self, timeout: int) -> str:
        """Прочитать новый вывод без отправки ввода."""
        async with self.lock:
            if self.proc is None:
                return "(терминал не запущен)"
            if not self._active:
                return self._take_text() if self._buf else "(нет нового вывода)"
            out = await self._collect(timeout)
            return "(нет нового вывода)" if out == "(пустой вывод)" else out


_TERMINALS: dict[str, Terminal] = {}
_MAX_TERMINALS = 64  # потолок живых терминалов; самый старый убивается (F11)


def _terminal(tid: str) -> Terminal:
    return _TERMINALS.setdefault(tid, Terminal())


async def _evict_if_needed(tid: str) -> None:
    if tid not in _TERMINALS and len(_TERMINALS) >= _MAX_TERMINALS:
        oldest = next(iter(_TERMINALS))
        await _TERMINALS.pop(oldest)._kill()


async def exec_in(tid: str, command: str, timeout: int) -> str:
    await _evict_if_needed(tid)
    return await _terminal(tid).exec(command, timeout)


async def input_in(tid: str, data: str, enter: bool, timeout: int) -> str:
    await _evict_if_needed(tid)
    return await _terminal(tid).input(data, enter, timeout)


async def poll_in(tid: str, timeout: int) -> str:
    await _evict_if_needed(tid)
    return await _terminal(tid).poll(timeout)


async def kill(tid: str) -> bool:
    """Убить терминал сессии (cancel прогона — убивает и дочерний shell)."""
    term = _TERMINALS.pop(tid, None)
    if term is None:
        return False
    await term._kill()
    return True


async def close_all() -> None:
    for term in _TERMINALS.values():
        await term._kill()
    _TERMINALS.clear()
