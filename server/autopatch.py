"""autopatch.py — патч прогона доработки применяется сам (goal 2026-10-21).

Раньше патч ждал ручного apply владельцем; до 01.11 патч применялся git apply --3way
прямо в боевое дерево. После двух столкновений с параллельной разработкой интеграция
идёт через изолированный worktree (слово владельца 01.11 «сделай чтобы параллельная
доработка была возможна»):

    fetch origin → worktree от origin/main (temp-каталог) → git apply --3way
    → smoke внутри worktree → commit → push; гонка push (origin уехал) —
      весь цикл повторяется на новой базе (devrun.rebase_retries)
    → боевое дерево только fast-forward → detached autorestart
       (откат при непрошедшем health = git revert, история общая).

Что это чинит (бои 01.11.2026):
* прогон/ручная сессия на устаревшей базе затирали свежие коммиты
  (deb3e8a/b7a38b3 дважды затёрли серверные правки) — теперь патч кладётся
  на СВЕЖИЙ origin/main или не кладётся вовсе;
* неудачный `git apply --3way` оставлял в боевом дереве unmerged-индекс и
  маркеры конфликта (dc6b05: битый клиент в раздаче, git блокировал любой
  коммит) — теперь конфликт возможен только в одноразовом worktree;
* устаревшая база (stale=True) — devexec переисполняет прогон на свежей
  базе (devrun.stale_retries), задача не теряется.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger("autopatch")

ROOT = Path(__file__).resolve().parents[1]


def _git_bin() -> str:
    """git из PATH сервиса; /usr/bin/git на Маке без Xcode CLT отвечает
    «xcode-select: error», поэтому homebrew проверяется явно (как в devexec)."""
    found = shutil.which("git")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/git", "/usr/local/bin/git"):
        if Path(cand).exists():
            return cand
    return "git"


GIT = _git_bin()


def _git(args, stdin: str | None = None, timeout: int = 120, cwd=None):
    return subprocess.run([GIT, *args], cwd=str(cwd or ROOT), input=stdin,
                          capture_output=True, text=True, timeout=timeout)


def patch_files(patch: str) -> list[str]:
    """Файлы из заголовков диффа (b/...), в порядке появления."""
    out: list[str] = []
    for m in re.finditer(r"^\+\+\+ b/(.+?)\s*$", patch, re.M):
        f = m.group(1).strip()
        if f and f not in out:
            out.append(f)
    return out


def _dirty(files: list[str]) -> list[str]:
    """Какие из файлов патча уже имеют незакоммиченные правки."""
    if not files:
        return []
    st = _git(["status", "--porcelain", "--", *files])
    return [ln[3:].strip() for ln in st.stdout.splitlines() if ln.strip()]


_REJECT_GLOBS = ("*.rej", "*.orig")


def clean_rejects() -> list[str]:
    """Убрать мусор неудачного apply (*.rej/*.orig) из боевого дерева.

    Исторически `git apply --3way` в боевом дереве оставлял рядом с целевым
    файлом `<имя>.rej`; с 01.11 интеграция идёт в одноразовом worktree и мусор
    там не появляется, но старые *.rej и CI-деплой, который отменяется на
    непустом `git status`, никуда не делись — уборка остаётся.
    """
    out = _git(["ls-files", "--others", "--exclude-standard", "--",
                *_REJECT_GLOBS])
    removed: list[str] = []
    for line in (out.stdout or "").splitlines():
        rel = line.strip()
        if not rel:
            continue
        p = ROOT / rel
        try:
            if p.is_file():
                p.unlink()
                removed.append(rel)
        except OSError as e:
            log.warning("автопатч: не удалить %s: %r", rel, e)
    if removed:
        log.warning("убран мусор неудачного apply: %s", ", ".join(removed[:8]))
    return removed


def _run_task(rid: str) -> str:
    """Текст задачи прогона из runs.inp — то, что писал владелец."""
    try:
        import json
        from . import db
        row = db.get_db().execute(
            "SELECT inp FROM runs WHERE id LIKE ?", (str(rid) + "%",)).fetchone()
        if row:
            inp = json.loads(row["inp"] or "{}") or {}
            return str(inp.get("task") or "")
    except Exception as e:  # noqa: BLE001 — тема коммита не роняет автопатч
        log.warning("автопатч %s: задачу прогона не прочитал: %r", rid, e)
    return ""


def _first_line(text: str, cap: int = 70) -> str:
    """Первая содержательная строка: без маркеров списка, пробелы схлопнуты,
    обрезка по границе слова (тема коммита не должна обрываться на полуслове)."""
    for raw in (text or "").splitlines():
        raw = " ".join(raw.strip().strip("#-•*").split())
        if not raw:
            continue
        if len(raw) <= cap:
            return raw
        cut = raw[:cap]
        sp = cut.rfind(" ")
        if sp > cap // 2:
            cut = cut[:sp]
        return cut.rstrip(" ,;:(«") + "…"
    return ""


def _subject(rid: str, summary: str, files: list[str] | None = None) -> str:
    """Тема коммита: суть задачи, а не первая фраза отчёта исполнителя.

    Порядок источников: задача прогона → отчёт исполнителя → список файлов.
    """
    line = _first_line(_run_task(rid)) or _first_line(summary)
    if not line:
        names = [Path(f).name for f in (files or [])][:3]
        line = (f"{len(files or [])} файл(ов): " + ", ".join(names)) if names \
            else "доработка"
    return f"devrun {str(rid)[:12]}: {line[:70]}"


# ─────────────────────── интеграционный worktree ───────────────────────

def _int_worktree(rid: str, attempt: int) -> Path:
    """Одноразовый worktree от origin/main — вне боевого дерева (temp).

    Всё грязное (apply, возможные конфликты, smoke) происходит здесь; боевое
    дерево получает только готовый коммит через fast-forward.
    """
    d = Path(tempfile.mkdtemp(prefix=f"ns-int-{str(rid)[:8]}-{attempt}-"))
    d.rmdir()  # worktree add требует, чтобы каталога ещё не было
    r = _git(["worktree", "add", "--detach", str(d), "origin/main"], timeout=240)
    if r.returncode != 0:
        raise RuntimeError("git worktree add: " + (r.stderr or r.stdout).strip()[:300])
    return d


def _int_cleanup(wt) -> None:
    if wt is None:
        return
    try:
        _git(["worktree", "remove", "--force", str(wt)], timeout=120)
        _git(["worktree", "prune"], timeout=60)
    except Exception as e:  # noqa: BLE001 — уборка не роняет отчёт
        log.warning("автопатч: интеграционный worktree %s не убран: %r", wt, e)


def _smoke_in(wt: Path, timeout: int = 300) -> tuple[bool, str]:
    """smoke.py внутри интеграционного worktree — проверяет СОБРАННЫЙ код
    (origin/main + патч), а не боевое дерево. Нет скрипта (голый тестовый репо)
    — гейт пропускается."""
    script = wt / "scripts" / "smoke.py"
    if not script.exists():
        return True, "smoke.py не найден — гейт пропущен"
    env = {**os.environ, "PYTHONUTF8": "1"}
    try:
        p = subprocess.run([sys.executable, str(script)], cwd=str(wt),
                           capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, f"smoke не завершился за {timeout} с"
    out = (p.stdout or "") + (p.stderr or "")
    ok = p.returncode == 0 and "SMOKE PASS" in out
    tail = " | ".join(ln.strip() for ln in out.strip().splitlines()[-5:])
    return ok, f"rc={p.returncode} {tail}"[:600]


def apply(rid: str, patch: str, summary: str = "", do_restart: bool | None = None) -> dict:
    """Интегрировать патч прогона: worktree от свежего origin/main → apply →
    smoke → commit → push (цикл с ребейзом при гонке) → ff боевого дерева →
    рестарт. Отчёт: applied/committed/pushed/restart/files/smoke/error/rebased/stale.

    Боевое дерево не участвует в интеграции вовсе: при устаревшей базе
    (stale=True) и при любом провале оно остаётся нетронутым — никаких
    unmerged-записей и маркеров конфликта (бой dc6b05 01.11).
    """
    res = {"rid": rid, "applied": False, "committed": False, "pushed": False,
           "restart": False, "files": [], "smoke": "", "error": "", "rejected": [],
           "rebased": 0, "stale": False, "already": False, "verified": False,
           "commit_sha": "", "prev_sha": ""}
    if not (patch or "").strip():
        res["error"] = "пустой патч"
        return res

    from .config import load_config
    cfg = load_config()
    dc = getattr(cfg, "devrun", None)
    if dc is not None and not getattr(dc, "auto_apply", True):
        res["error"] = "auto_apply выключен в конфиге"
        return res

    # старый мусор из боевого дерева — до всего (CI-деплой отменяется на untracked)
    res["rejected"] = clean_rejects()

    files = patch_files(patch)
    res["files"] = files

    # Грязное боевое дерево = чья-то незакоммиченная работа (ручная сессия):
    # ff-синхронизация её бы затёрла. Не интегрируем, ничего не трогаем (A3/F5).
    dirty = _dirty(files)
    if dirty and not bool(getattr(dc, "allow_dirty", False)):
        res["error"] = ("дерево грязное — патч не применён (сначала закоммить: "
                        + ", ".join(dirty[:6]) + ")")
        log.warning("автопатч %s: %s", rid, res["error"])
        return res

    retries = max(0, int(getattr(dc, "rebase_retries", 2)))
    pushed_sha = ""
    already = False
    for attempt in range(retries + 1):
        f = _git(["fetch", "origin", "--prune"], timeout=240)
        if f.returncode != 0:
            res["error"] = "git fetch origin: " + (f.stderr or f.stdout).strip()[:300]
            return res
        wt = None
        try:
            wt = _int_worktree(rid, attempt)
            ap = _git(["apply", "--3way", "--whitespace=nowarn"], stdin=patch, cwd=wt)
            if ap.returncode != 0:
                rev = _git(["apply", "-R", "--check", "--3way", "--whitespace=nowarn"],
                           stdin=patch, cwd=wt)
                if rev.returncode == 0:
                    # Обратный apply лёг бы чисто = патч УЖЕ в origin/main: прошлая
                    # интеграция умерла после push, либо recovery поднял давно
                    # интегрированный прогон. Ничего не применяем — только синхронизация.
                    already = True
                    break
                # База устарела: origin/main уехал и патч не лёг даже 3way.
                # Конфликт остался в одноразовом worktree — боевое дерево цело.
                # devexec по stale=True переисполнит прогон на свежей базе.
                res["stale"] = True
                res["error"] = ("устаревшая база: патч не лёг на свежий origin/main — "
                                + (ap.stderr or ap.stdout).strip()[:300])
                return res
            if bool(getattr(dc, "smoke_gate", True)):
                ok, detail = _smoke_in(wt, int(getattr(dc, "smoke_timeout", 300)))
                res["smoke"] = detail
                if not ok:
                    res["error"] = "smoke не прошёл — интеграция отменена: " + detail[-300:]
                    log.warning("автопатч %s: %s", rid, res["error"])
                    return res
            add = _git(["add", "-A", "--", "."], cwd=wt)
            if add.returncode != 0:
                res["error"] = "git add: " + (add.stderr or "").strip()[:300]
                return res
            cargs = ["commit", "-m", _subject(rid, summary, files)]
            body = (summary or "").strip()[:1500]
            if body:
                cargs += ["-m", body]
            cm = _git(cargs, cwd=wt)
            if cm.returncode != 0:
                res["error"] = "git commit: " + (cm.stderr or cm.stdout).strip()[:300]
                return res
            sha = _git(["rev-parse", "HEAD"], cwd=wt).stdout.strip()
            push = _git(["push", "origin", "HEAD:main"], timeout=180, cwd=wt)
            if push.returncode == 0:
                pushed_sha = sha
                break
            # гонка push: origin/main уехал, пока шла интеграция, — цикл
            # повторится на новой базе (это и есть авто-rebase)
            res["rebased"] += 1
            log.info("автопатч %s: push-гонка (попытка %d) — ребейз на новый origin/main",
                     rid, attempt + 1)
        except Exception as e:  # noqa: BLE001 — отчёт важнее точности исключения
            res["error"] = f"интеграция: {type(e).__name__}: {e}"[:400]
            return res
        finally:
            _int_cleanup(wt)
    if already:
        res.update(applied=True, already=True, error="")
        # Обратный apply лёг бы чисто = патч УЖЕ в origin/main: это и есть
        # внешний факт интеграции, отдельная проверка не нужна.
        res["verified"] = True
        if not res["smoke"]:
            res["smoke"] = "патч уже в origin/main — гейт не гоняли"
    elif not pushed_sha:
        res["error"] = (f"push-гонка: {res['rebased']} ребейза подряд — origin/main "
                        "движется быстрее интеграции")
        return res
    else:
        res.update(applied=True, committed=True, pushed=True, commit_sha=pushed_sha[:12])
        # rc=0 пуша — ещё не факт: на Маке push печатает «failed to store: -25308»
        # (кейчейн) и всё равно проходит, а при гонке origin/main мог уехать.
        # Проверяем коммит в origin/main явно — «done» не должен врать.
        res["verified"] = verify_in_origin(pushed_sha)
        if not res["verified"]:
            res["error"] = (f"push вернулся rc=0, но коммит {pushed_sha[:12]} не найден "
                            "в origin/main — интеграция не подтверждена")
            log.warning("автопатч %s: %s", rid, res["error"])
            return res

    # боевое дерево: только fast-forward (локальные непушнутые коммиты ручной
    # сессии — rebase поверх; конфликт = дерево не трогаем, сервис на старом коде)
    head_before = _git(["rev-parse", "HEAD"]).stdout.strip()
    res["prev_sha"] = head_before[:12]
    ff = _git(["merge", "--ff-only", "origin/main"])
    if ff.returncode != 0:
        rb = _git(["rebase", "origin/main"])
        if rb.returncode != 0:
            _git(["rebase", "--abort"])
            res["restart"] = False
            res["error"] = ("интегрировано в origin/main (" + pushed_sha[:12] + "), но "
                            "боевое дерево не синхронизировано: локальные коммиты "
                            "конфликтуют — сервис на старом коде, нужен ручной pull/rebase")
            log.warning("автопатч %s: %s", rid, res["error"])
            try:
                from . import alerts
                alerts.send("warning", "autopatch_sync", f"{rid[:8]}: {res['error']}")
            except Exception:  # noqa: BLE001
                pass
            return res

    head_after = _git(["rev-parse", "HEAD"]).stdout.strip()
    if already and head_after == head_before:
        return res   # код не изменился (патч уже в origin, дерево было синхронно)

    if do_restart is None:
        do_restart = bool(getattr(dc, "auto_restart", True))
    if not do_restart:
        return res

    pdir = Path(str(getattr(cfg, "data_dir", "data"))) / "devruns"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{rid}.patch").write_text(patch, encoding="utf-8")

    # Не убивать сервис под живым ходом владельца (у dsh: «Shutdown waits up to
    # 10 seconds for each worker's background work»): обрыв хода на полуслове
    # выглядит как «сбой связи» и портит прибор качества.
    grace = max(0.0, float(getattr(dc, "restart_grace_seconds", 15) or 0))
    _wait_idle(grace)

    scfg = cfg.server
    cmd = [sys.executable, "-m", "uvicorn", "server.main:app",
           "--host", scfg.host, "--port", str(scfg.port)]
    helper = ROOT / "scripts" / "autorestart.py"
    args = [sys.executable, str(helper), "--pid", str(_server_pid()),
            "--port", str(scfg.port), "--rid", rid,
            "--rollback", pushed_sha, "--prev", res["prev_sha"],
            # рестарт ждёт маркер, который ставит main._mark_notified: без него
            # autorestart убивал процесс через 1,5 с и финал прогона не доезжал
            # до чата (замер 02.11: 12 из 15 вердиктов судьи — «результат в
            # транскрипте не подтверждён»)
            "--wait-file", str(pdir / f"{rid}.notified"),
            "--grace", f"{max(grace, 15.0):.1f}"]
    args.append("--")
    args += cmd
    try:
        with open(ROOT / "noosphere.log", "ab") as fh:
            proc = subprocess.Popen(args, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=fh, start_new_session=True,
                                    close_fds=True)
        res["restart"] = proc.pid > 0
        log.info("автопатч %s: перезапуск через %s (pid %s, rollback=%s)",
                 rid, helper.name, proc.pid, pushed_sha[:12])
    except Exception as e:  # noqa: BLE001
        res["error"] = (res["error"] + " | " if res["error"] else "") + f"restart: {e}"
    return res


def verify_in_origin(sha: str) -> bool:
    """Факт интеграции: коммит действительно в origin/main.

    `rc=0` от push — ещё не факт: на Маке push печатает «fatal: failed to store:
    -25308» (сбой записи в кейчейн) и при этом проходит, а при гонке origin/main
    мог уехать. Проверяем внешним наблюдаемым фактом (у dsh-superagent то же
    правило: «Ticket is done when none of the pull requests it opened remain
    open» — статус не верит самому себе).
    """
    if not sha:
        return False
    _git(["fetch", "origin", "--prune"], timeout=240)
    r = _git(["merge-base", "--is-ancestor", sha, "origin/main"], timeout=60)
    return r.returncode == 0


def _wait_idle(grace: float) -> float:
    """Подождать до grace секунд, пока в процессе нет активных чат-ходов.

    Возвращает фактическое время ожидания. BUSY читаем отложенным импортом:
    autopatch зовут и из smoke, где приложения может не быть.
    """
    t0 = time.time()
    if grace <= 0:
        return 0.0
    try:
        from . import main as _main
    except Exception:  # noqa: BLE001 — нет приложения (smoke/скрипт)
        return 0.0
    while time.time() - t0 < grace:
        if not set(getattr(_main, "BUSY", ()) or ()):
            break
        time.sleep(0.5)
    waited = time.time() - t0
    if waited > 1.0:
        log.info("автопатч: подождал живые ходы %.1f с перед рестартом", waited)
    return waited


def _server_pid() -> int:
    """pid текущего uvicorn-сервера (родитель autorestart убьёт его)."""
    try:
        out = subprocess.run(["pgrep", "-f", "uvicorn server.main:app"],
                             capture_output=True, text=True, timeout=10).stdout
        pids = [int(p) for p in out.split() if p.isdigit()]
        if pids:
            return min(pids)
    except Exception:  # noqa: BLE001
        pass
    return os.getpid()
