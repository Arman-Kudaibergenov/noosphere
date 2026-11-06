"""«Получить обновление» без перезапуска руками: git pull + перезагрузка страницы.

Оболочка и web-UI живут в рабочей копии репозитория на ноутбуке; сервер деплоится
своим CI на Маке, а клиентские файлы приезжают обычным `git pull --ff-only`
(только fast-forward: merge-конфликт не должен ронять приложение посередине).
Перезапуск процесса нужен лишь когда менялись `client/desktop/**` или точка входа:
новая копия стартует с ожиданием mutex (рукопожатие single_instance.acquire(wait_s)),
старая гаснет сама — владелец не выходит из окна и не ищет ярлык заново.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import appconfig, applog

PULL_TIMEOUT = 90.0
SHELL_PREFIXES = ("client/desktop/", "client/noosphere_app.pyw", "client/noosphere.pyw")


def _git(repo: Path, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout)


def pull(repo: Path | None = None) -> dict:
    """Fast-forward pull рабочей копии; возвращает сводку для журнала и тоста."""
    repo = repo or appconfig.REPO_ROOT
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    proc = _git(repo, "pull", "--ff-only", timeout=PULL_TIMEOUT)
    after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    out = {"ok": proc.returncode == 0, "before": before[:8], "after": after[:8],
           "files": [], "shell_changed": False,
           "output": (proc.stdout + proc.stderr).strip()[:400]}
    if not out["ok"]:
        applog.warn("update pull failed", error=out["output"])
        return out
    if before != after:
        diff = _git(repo, "diff", "--name-only", before, after)
        out["files"] = [l for l in diff.stdout.splitlines() if l.strip()]
        out["shell_changed"] = any(f.startswith(SHELL_PREFIXES) for f in out["files"])
    applog.info("update pulled", before=out["before"], after=out["after"],
                files=len(out["files"]), shell_changed=out["shell_changed"])
    return out


def restart(argv: list[str] | None = None) -> int:
    """Поднимает новую копию оболочки откреплённой и возвращает её pid."""
    args = [str(appconfig.ENTRY_PYW)] + list(argv or [])
    handle = subprocess.Popen([str(_pythonw()), *args],
                              creationflags=subprocess.DETACHED_PROCESS
                              | subprocess.CREATE_NEW_PROCESS_GROUP)
    applog.info("update restart spawned", pid=handle.pid, argv=" ".join(args))
    return handle.pid


def _pythonw() -> Path:
    sibling = Path(sys.executable).with_name("pythonw.exe")
    return sibling if sibling.exists() else Path(sys.executable)
