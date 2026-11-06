#!/usr/bin/env python3
"""autorestart.py — перезапуск сервера после автопатча (goal 2026-10-21).

Запускается detached из server/autopatch.py: ждёт, гасит текущий uvicorn,
поднимает новый код, опрашивает /health до 40с. Не поднялся — откатывает
патч (сброс коммита + обратный git apply) и возвращает прежний код.

usage: autorestart.py --pid N --port 8300 --patch <file> --rid <rid> [--commit] -- <cmd...>
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "noosphere.log"


def say(msg: str) -> None:
    print(f"autorestart: {msg}", flush=True)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill(pid: int, wait: float = 15.0) -> None:
    if pid <= 0 or not alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    t0 = time.time()
    while time.time() - t0 < wait:
        if not alive(pid):
            return
        time.sleep(0.3)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    time.sleep(0.5)


def start(cmd: list[str]) -> int:
    with open(LOG, "ab") as fh:
        p = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                             stdout=fh, stderr=fh, start_new_session=True,
                             close_fds=True)
    return p.pid


def healthy(port: int, timeout: float = 40.0) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if 200 <= r.status < 300:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)
    return False


def git(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(ROOT), input=stdin,
                          capture_output=True, text=True, timeout=120)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--port", type=int, default=8300)
    ap.add_argument("--patch", default="")
    ap.add_argument("--rid", default="")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--rollback", default="")   # sha интеграционного коммита (откат = revert)
    ap.add_argument("--prev", default="")       # HEAD боевого дерева до интеграции
    ap.add_argument("--wait-file", default="")  # маркер «чат уведомлён»: ждём его до --grace
    ap.add_argument("--grace", type=float, default=15.0)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = [c for c in a.cmd if c != "--"]
    if not cmd:
        say("нет команды запуска")
        return 2

    # Убийство сервера через 1,5 с рвало уведомление чата: main._notify_devrun
    # пишет заметку ПОСЛЕ autopatch.apply, а apply уже поднял этот скрипт
    # (замер 02.11 — судья назвал «результат в транскрипте не подтверждён»
    # 12 чатов из 15). Ждём маркер, который ставит уведомление; не появился —
    # всё равно рестартуем, но не раньше --grace секунд.
    if a.wait_file:
        t0 = time.time()
        marker = Path(a.wait_file)
        while time.time() - t0 < a.grace:
            if marker.exists():
                say(f"rid={a.rid} чат уведомлён через {time.time() - t0:.1f} с")
                break
            time.sleep(0.2)
        else:
            say(f"rid={a.rid} маркер {marker.name} не появился за {a.grace:.0f} с"
                " — рестартую без него")
    else:
        time.sleep(1.5)
    kill(a.pid)
    pid = start(cmd)
    say(f"rid={a.rid} новый сервер pid={pid}, ждём /health")
    if healthy(a.port):
        if not alive(pid):
            # Порт отвечает, но НЕ наш процесс: его поднял cron-keepalive
            # (noosphere-start.sh), пока мы ждали health, а наш умер с
            # «address already in use» (бой 02.11, rid=95be9f34a99a: pid 70960
            # не смог занять 8300, сервером стал 70772). Код в дереве уже новый,
            # поэтому откатывать нельзя — revert убил бы хороший коммит.
            say(f"rid={a.rid} ok, но порт держит другой процесс (наш pid={pid} умер —"
                " вероятно, cron-keepalive успел первым); откат НЕ делаем")
            return 0
        say(f"rid={a.rid} ok, сервер на новом коде")
        return 0

    say(f"rid={a.rid} новый код не поднялся — откат")
    kill(pid)
    if a.rollback:
        # интеграционный коммит уже в origin/main: откатываемся revert'ом —
        # история общая, на ней работают параллельные сессии (reset её ломает).
        r = git("revert", "--no-edit", a.rollback)
        say(f"revert {a.rollback[:12]} rc={r.returncode} {r.stderr.strip()[:200]}")
        if r.returncode == 0:
            p = git("push", "origin", "HEAD:main")
            say(f"push revert rc={p.returncode} {(p.stderr or '').strip()[:200]}")
        else:
            git("revert", "--abort")
            if a.prev:
                r2 = git("reset", "--hard", a.prev)
                say(f"revert конфликтует — ЛОКАЛЬНЫЙ reset --hard {a.prev[:12]} "
                    f"rc={r2.returncode}; в origin/main остался плохой коммит "
                    f"{a.rollback[:12]} — нужно ручное вмешательство")
    else:
        patch = Path(a.patch) if a.patch else None
        if patch and patch.exists():
            text = patch.read_text(encoding="utf-8")
            if a.commit:
                r = git("reset", "--mixed", "HEAD~1")
                say(f"reset HEAD~1 rc={r.returncode} {r.stderr.strip()[:200]}")
            r = git("apply", "-R", "--3way", "--whitespace=nowarn", stdin=text)
            say(f"git apply -R rc={r.returncode} {(r.stderr or '').strip()[:200]}")
    pid2 = start(cmd)
    say(f"rid={a.rid} прежний код поднят pid={pid2}")
    return 0 if healthy(a.port) else 1


if __name__ == "__main__":
    sys.exit(main())
