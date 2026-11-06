"""devrun.py — исполнитель прогонов доработки на ноутбуке (goal 2026-10-12).

Прогон: изолированный worktree от origin/main + headless Qwen Code
(approval-mode auto-edit: правки файлов автоодобрены, shell-команды — нет),
журнал `%LOCALAPPDATA%\\Noosphere\\devruns\\<rid>.log`. Сам прогон не коммитит
и не пушит: пост-проверка сравнивает HEAD worktree с base и отклоняет прогон,
который сделал коммит.
Apply патча — вручную владельцем (механизм заявок approvals удалён решением
владельца 2026-10-17): патч лежит на сервере в карточке прогона.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

from . import appconfig, applog, httpapi

QWEN_CMD = r"C:\Users\Arman\AppData\Roaming\npm\qwen.cmd"
MAX_WALL = "18m"
MAX_TURNS = 60

RULES = ("\n\nПравила прогона: правь только файлы; не запускай shell-команды; "
         "не выполняй git add/commit/push; не читай ~/.secrets, *.token и значения "
         "токенов; оставь правки незакоммиченными; в финальном сообщении перечисли "
         "изменённые файлы и суть правки одной-двумя фразами.")


def patch_hash(patch: str) -> str:
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()[:16]


def runs_dir() -> Path:
    path = appconfig.home_dir() / "devruns"
    path.mkdir(parents=True, exist_ok=True)
    return path


def worktree_path(rid: str) -> Path:
    return appconfig.REPO_ROOT / ".qwen" / "devruns" / rid


def log_path(rid: str) -> Path:
    return runs_dir() / f"{rid}.log"


def read_log(rid: str, tail: int = 6000) -> str:
    try:
        text = log_path(rid).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-tail:]


def _project_dir_name(wt: Path) -> str:
    """Имя каталога сессии Qwen: cwd прогона нижним регистром, каждый
    не-латино-цифровой символ заменён дефисом (как именует сам Qwen)."""
    return re.sub(r"[^a-z0-9]", "-", str(wt).lower())


def _progress_lines(ev: dict) -> list[str]:
    """Событие session-jsonl прогона → человекочитаемые строки прогресса."""
    ts = (ev.get("timestamp") or "")[11:19]
    parts = ((ev.get("message") or {}).get("parts")) or []
    out: list[str] = []
    kind = ev.get("type")
    if kind == "assistant":
        for p in parts:
            fc = p.get("functionCall")
            if fc:
                args = json.dumps(fc.get("args") or {}, ensure_ascii=False)[:100]
                out.append(f"[{ts}] tool {fc.get('name')} {args}")
            elif p.get("text") and not p.get("thought"):
                out.append(f"[{ts}] {' '.join((p.get('text') or '').split())[:160]}")
    elif kind == "tool_result":
        status = ((ev.get("toolCallResult") or {}).get("status")) or "success"
        if status != "success":
            name, resp = "", ""
            for p in parts:
                fr = p.get("functionResponse") or {}
                name = fr.get("name") or name
                resp = json.dumps(fr.get("response") or {}, ensure_ascii=False)[:160]
            out.append(f"[{ts}] ! tool {name} {status}: {resp}")
    return out


def _tail_progress(wt: Path, fh, lock: threading.Lock,
                   stop: threading.Event) -> None:
    """Живой прогресс в журнале прогона: хвост session-jsonl этой сессии.
    Без него text-журнал молчит до финала и живой прогон выглядит мёртвым
    (жалоба владельца 2026-10-13: «снова ошибка с прогонами»)."""
    chats = Path.home() / ".qwen" / "projects" / _project_dir_name(wt) / "chats"
    path = None
    deadline = time.monotonic() + 90
    while path is None and not stop.is_set() and time.monotonic() < deadline:
        try:
            found = sorted(chats.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        except OSError:
            found = []
        path = found[-1] if found else None
        if path is None:
            stop.wait(1.0)
    if path is None:
        return
    with path.open("rb") as src:
        while not stop.is_set():
            raw = src.readline()
            if not raw:
                stop.wait(0.7)
                continue
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            for line in _progress_lines(ev):
                with lock:
                    fh.write(line + "\n")
                    fh.flush()


def _first_session_id(wt: Path) -> str:
    """ID первой сессии прогона (старейший jsonl каталога): ретрай возобновляет
    её вместо старта задачи с нуля."""
    chats = Path.home() / ".qwen" / "projects" / _project_dir_name(wt) / "chats"
    try:
        found = sorted(chats.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return ""
    return found[0].stem if found else ""


def _rmtree_force(path: Path) -> None:
    """rmtree на Windows спотыкается о read-only файлы в .git: без этого клон
    finished-прогона молча оставался в дереве (ignore_errors) и грязнил дерево
    владельца для гейта A2 (инцидент 13.10)."""
    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_onerror)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=180)


FINISH_HOOK = None  # noosphere_app подставляет тост о финише прогона


def _spawn(server: str, token: str, rid: str, base_sha: str, task: str) -> dict:
    """Клон-песочница прогона и поток headless-qwen; общий для кнопки, очереди
    и pickup чат-прогонов."""
    wt = worktree_path(rid)
    # локальный клон вместо worktree: у клона свой config — после
    # `remote remove origin` у прогона физически нет цели для push,
    # гейт A3 не обходится даже при --approval-mode yolo
    clone = _git(appconfig.REPO_ROOT, "clone", "--quiet", str(appconfig.REPO_ROOT), str(wt))
    if clone.returncode == 0:
        clone = _git(wt, "checkout", "--detach", base_sha or "HEAD")
    if clone.returncode == 0:
        clone = _git(wt, "remote", "remove", "origin")
    if clone.returncode != 0:
        shutil.rmtree(wt, ignore_errors=True)
        httpapi.request(server, f"/v1/devruns/{rid}/finish", token, method="POST",
                        body={"status": "error",
                              "summary": "клон каталога прогона не создался: " + clone.stderr.strip()[:200]})
        return {"ok": False, "error": clone.stderr.strip()[:300]}
    threading.Thread(target=_run, args=(server, token, rid, wt, base_sha, task),
                     name=f"devrun-{rid}", daemon=True).start()
    return {"ok": True, "rid": rid}


def start(server: str, token: str, task: str) -> dict:
    """Создать run на сервере и исполнить; ответ для моста."""
    base = _git(appconfig.REPO_ROOT, "rev-parse", "origin/main")
    base_sha = base.stdout.strip() if base.returncode == 0 else ""
    reply = httpapi.request(server, "/v1/devruns", token, method="POST",
                            body={"task": task, "base_sha": base_sha})
    if not reply.ok or not isinstance(reply.data, dict):
        return {"ok": False, "error": reply.error or "сервер не создал прогон"}
    rid = str(reply.data.get("run_id") or "")
    return _spawn(server, token, rid, base_sha, task)


def pickup_once(server: str, token: str) -> None:
    """Забрать прогоны, поставленные из чата (инструмент devrun_request):
    сервер отдал queued-строки с source=chat:*, атомарный pickup защищает от
    двойного захвата. Точка входа «открыл чат — попросил доработать»."""
    reply = httpapi.request(server, "/v1/devruns/pending-chat", token)
    if not reply.ok or not isinstance(reply.data, list):
        return
    for row in reply.data:
        rid = str(row.get("id") or "")
        task = str(row.get("task") or "").strip()
        if not rid or not task:
            continue
        pk = httpapi.request(server, f"/v1/devruns/{rid}/pickup", token,
                             method="POST")
        if not pk.ok or not (pk.data or {}).get("ok"):
            continue
        base = _git(appconfig.REPO_ROOT, "rev-parse", "origin/main")
        base_sha = base.stdout.strip() if base.returncode == 0 else ""
        applog.info("devrun pickup from chat", rid=rid, task=task[:80])
        _spawn(server, token, rid, base_sha, task)


def _attempt(rid: str, wt: Path, cmd: list[str], fh, lock: threading.Lock) -> int:
    """Одна попытка headless-прогона; stdout насосом идёт в общий журнал fh."""
    proc = None
    pump = None
    try:
        # stdin=DEVNULL: закрытый stdin pythonw headless-qwen читал как ^C
        # (rc=0xC000013A, прогон 43a72b6e747c). CREATE_NO_WINDOW обязателен:
        # без него .cmd-обёртка поднимает видимую консоль, и её закрытие
        # шлёт прогону CTRL_C (прогон 2cf221b33922 умер так на 6-й минуте);
        # «виснутые» прогоны 9208/743e/35b6 — эпоха auto-edit (approval
        # hang), при yolo CREATE_NO_WINDOW не вредит
        proc = subprocess.Popen(cmd, cwd=str(wt), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                creationflags=0x08000000,
                                env=dict(os.environ, QWEN_CODE_DEBUG="1"))

        def _pump() -> None:
            for raw in proc.stdout:
                with lock:
                    fh.write(raw.decode("utf-8", "replace"))
                    fh.flush()

        pump = threading.Thread(target=_pump, daemon=True, name=f"pump-{rid}")
        pump.start()
        # 1100 с > wall-cap 18 мин: убитый капом прогон всё равно оставляет
        # правки в клоне — их заберёт сбор патча ниже
        return proc.wait(timeout=1100)
    except subprocess.TimeoutExpired:
        if proc:
            # kill у .cmd-обёртки оставляет node-внуков: бьём по дереву
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
        return -2
    except OSError as exc:
        with lock:
            fh.write(f"\n# spawn failed: {exc!r}\n")
        return -3
    finally:
        if pump:
            pump.join(timeout=5)


def _run(server: str, token: str, rid: str, wt: Path, base_sha: str, task: str) -> None:
    # без --mcp-config: санбокс-проба с ним прошла, а прогон 9208cf8a900a с ним
    # повис до первого хода; предупреждение о node-repl безвредно
    # yolo: в headless любой инструмент, требующий approval (agent и др.),
    # вешает прогон навсегда (Warning: Tool "agent" requires user approval…);
    # безопасность держит не approval-mode, а клон без origin + пост-проверка
    cmd = [QWEN_CMD, task + RULES, "--approval-mode", "yolo",
           "--max-wall-time", MAX_WALL, "--max-session-turns", str(MAX_TURNS),
           "--output-format", "text"]
    applog.info("devrun started", rid=rid, base=base_sha[:8])
    stop = threading.Event()
    lock = threading.Lock()
    with log_path(rid).open("w", encoding="utf-8", errors="replace") as fh:
        fh.write(f"# devrun {rid}\n# base {base_sha}\n# task {task[:200]}\n")
        # журнал живёт весь прогон: прогресс сессии и stdout qwen идут в один
        # fh под одним замком — иначе живой прогон выглядит мёртвым
        tailer = threading.Thread(target=_tail_progress,
                                  args=(wt, fh, lock, stop),
                                  daemon=True, name=f"tail-{rid}")
        tailer.start()
        rc = _attempt(rid, wt, cmd, fh, lock)
        if rc in (-2, 55):
            applog.warn("devrun transient failure, retrying once", rid=rid, rc=rc)
            sid = _first_session_id(wt)
            if sid:
                # resume: ретрай продолжает убитую сессию и видит её правки в
                # клоне; без этого крупная задача стартует с нуля (инцидент
                # 34ffb1a827f1: попытка 1 умерла на капе, проработав 18 минут)
                cmd = cmd + ["--resume", sid]
            with lock:
                fh.write(f"# retry rc={rc}"
                         + (f" resume={sid}" if sid else "") + "\n")
            rc = _attempt(rid, wt, cmd, fh, lock)
        stop.set()
        tailer.join(timeout=3)
        fh.flush()
    head = _git(wt, "rev-parse", "HEAD").stdout.strip()
    committed = bool(base_sha) and head != base_sha
    patch = ""
    if not committed:
        # патч собираем при любом rc: убитый капом/таймаутом прогон всё равно
        # мог оставить полезные правки (инцидент 4d6c6f2ec35b)
        _git(wt, "add", "-A")
        patch = _git(wt, "diff", "--cached", "--binary", "--no-color").stdout
        _git(wt, "reset", "-q")
    status = "done" if rc == 0 and not committed else "error"
    lines = [ln for ln in read_log(rid, 4000).splitlines()
             if ln.strip() and not ln.startswith("#")]
    # строки прогресса начинаются с "[hh:mm:ss]": в summary идёт финал qwen,
    # а не хвост прогресса; при пустом финале — последняя осмысленная строка
    final = [ln for ln in lines if not ln.startswith("[")]
    summary = (final[-1] if final else (lines[-1] if lines else ""))[:300]
    if rc == -2:
        # финала не будет: прогон убит капом попытки на полном ходу — сказать
        # об этом прямо и показать последний шаг, а не warning о node-repl
        summary = ("не уложился в лимит попытки (~18 мин); последний шаг: "
                   + (lines[-1] if lines else ""))[:300]
    if committed:
        summary = "прогон сделал коммит в worktree — отклонён пост-проверкой"
    httpapi.request(server, f"/v1/devruns/{rid}/finish", token, method="POST",
                    body={"status": status, "summary": summary, "patch": patch,
                          "patch_hash": patch_hash(patch) if patch else ""})
    # каталог прогона больше не нужен: патч ушёл на сервер, дерево владельца
    # обязано оставаться чистым (гейт apply)
    _rmtree_force(wt)
    applog.info("devrun finished", rid=rid, rc=rc, committed=committed,
                patch_bytes=len(patch))
    if FINISH_HOOK:
        try:
            FINISH_HOOK(rid, status, summary)
        except Exception as exc:  # тост не должен ронять финиш прогона
            applog.warn("devrun finish hook failed", rid=rid,
                        error=f"{type(exc).__name__}: {exc}")
    if patch and not committed:
        # патч ждёт ручного apply владельцем на сервере (заявки удалены 2026-10-17)
        applog.info("devrun patch ready on server, apply вручную", rid=rid,
                    patch_bytes=len(patch))


def queue_dir() -> Path:
    path = appconfig.home_dir() / "devrun-queue"
    path.mkdir(parents=True, exist_ok=True)
    return path


def drain_queue(server: str, token: str) -> None:
    """Файл .task в очереди = прогон, созданный процессом окна (тот же путь,
    что кнопка «В прогон»): автономная постановка без вставки текста в UI."""
    for f in sorted(queue_dir().glob("*.task")):
        try:
            task = f.read_text(encoding="utf-8").strip()
            f.unlink()
        except OSError:
            continue
        if not task:
            continue
        out = start(server, token, task)
        applog.info("devrun queued start", file=f.name,
                    rid=out.get("rid") or "-", ok=out.get("ok"),
                    error=out.get("error") or "-")


def apply_watcher(endpoint, stop: threading.Event, interval: float = 20.0) -> None:
    applog.info("devrun apply watcher started", interval=interval)
    while not stop.is_set():
        server, token = endpoint()
        if server:
            try:
                pickup_once(server, token)
                drain_queue(server, token)
            except Exception as exc:
                applog.error("devrun apply watcher crashed",
                             error=f"{type(exc).__name__}: {exc}")
        stop.wait(interval)
    applog.info("devrun apply watcher stopped")
