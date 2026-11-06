"""devexec.py — исполнитель прогонов доработки НА СЕРВЕРЕ (решение владельца 2026-10-31).

«Всё на Маке»: раньше прогон исполняла оболочка на ноутбуке (client/desktop/devrun.py),
причём base_sha и worktree брались из её рабочей копии — форка репозитория GitLab
Биокарда (проект 534), тогда как автопатч применяет патч к боевому дереву gitea
admin/noosphere. Контекст не совпадал: `git apply --check` отвечал
«patch failed: server/memory.py:11», поэтому в истории дерева нет ни одного коммита
автопатча, а неудачный apply оставлял в боевом дереве `.rej` — untracked-файл,
из-за которого CI-деплой отменяется ещё до `git pull`.

Теперь прогон исполняет сам сервис:
    fetch origin → worktree от origin/main в ~/workspace/noosphere-dev/<rid>
    → агентский ход собственным циклом (pool.ask + tools: те же гварды — джейл путей,
      deny-list команд, журнал tool_audit, бюджеты хода)
    → git add -A + git diff --cached = патч
    → devruns.finish → autopatch.apply (smoke-гейт, commit, push, рестарт с откатом)
    → git worktree remove

Worktree живёт в ~/workspace — это каталог из allowlist записи файловых инструментов,
поэтому ослаблять джейл не нужно: ~/noosphere остаётся для агента недостижимым, а
боевое дерево правит только autopatch.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

from . import devruns, pool, tools
from .config import load_config

log = logging.getLogger("devexec")

ROOT = Path(__file__).resolve().parents[1]

EXEC_SYSTEM = (
    "Ты — исполнитель прогона доработки сервиса Ноосфера. Работаешь в изолированном "
    "worktree боевого репозитория; путь к нему дан в задаче, все правки — ТОЛЬКО внутри "
    "него.\n"
    "Порядок: прочитай затронутые файлы, сделай правку, перечитай результат и убедись, что "
    "он цел (Python — python3 -m py_compile, текст/разметка — закрытые теги и связность).\n"
    "Запрещено: git add/commit/push/checkout/reset/stash, выход за пределы worktree, "
    "чтение секретов и ~/.ssh, установка пакетов, запуск и перезапуск боевого сервиса, "
    "правка data/*.db и config.yaml.\n"
    "Патч снимет сервер сам и сам же его применит: smoke-гейт, commit, push, рестарт с "
    "откатом. Согласование с владельцем не требуется — не обещай его и не останавливайся "
    "в ожидании ответа.\n"
    "Финальный ответ коротко: какие файлы изменены, суть правки одной-двумя фразами и что "
    "проверено. Процесс и промежуточные вызовы не пересказывай."
)

# состав хода исполнителя: без рекурсии (devrun_request породил бы прогон из прогона)
# и без под-агентов (delegate удваивает расход и уводит правку чужим контекстом)
_EXCLUDED_TOOLS = ("devrun_request", "delegate")


class DevExecError(RuntimeError):
    """Исполнитель не смог довести прогон до патча."""


def _git_bin() -> str:
    """git из PATH сервиса; /usr/bin/git не берём — на Маке нет Xcode CLT и он
    отвечает «xcode-select: error: No developer tools were found»."""
    found = shutil.which("git")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/git", "/usr/local/bin/git"):
        if Path(cand).exists():
            return cand
    return "git"


GIT = _git_bin()


def _git(args: list[str], cwd: Path | None = None, stdin: str | None = None,
         timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run([GIT, *args], cwd=str(cwd or ROOT), input=stdin,
                          capture_output=True, text=True, timeout=timeout)


def worktree_root() -> Path:
    return Path(load_config().devrun.worktree_root).expanduser()


def worktree_path(rid: str) -> Path:
    return worktree_root() / str(rid)[:12]


def prepare(rid: str) -> tuple[Path, str]:
    """fetch + worktree от origin/main. Возвращает (путь worktree, base_sha).

    База — origin/main ТОГО ЖЕ репозитория, к которому автопатч применит патч:
    именно расхождение баз ломало последнюю милю самоправки.
    """
    f = _git(["fetch", "origin", "--prune"], timeout=240)
    if f.returncode != 0:
        raise DevExecError("git fetch origin: " + (f.stderr or f.stdout).strip()[:300])
    base = _git(["rev-parse", "origin/main"])
    if base.returncode != 0:
        raise DevExecError("git rev-parse origin/main: " + base.stderr.strip()[:200])
    wt = worktree_path(rid)
    if wt.exists():
        _git(["worktree", "remove", "--force", str(wt)], timeout=120)
    wt.parent.mkdir(parents=True, exist_ok=True)
    add = _git(["worktree", "add", "--detach", str(wt), "origin/main"], timeout=240)
    if add.returncode != 0:
        raise DevExecError("git worktree add: " + (add.stderr or add.stdout).strip()[:300])
    return wt, base.stdout.strip()


def extract_patch(wt: Path, max_chars: int = 400_000) -> str:
    """Патч всего, что исполнитель оставил в worktree (новые файлы включительно).

    `git add -A` обязателен: autopatch применяет патч через `git apply --3way`,
    а без индексации untracked-файлов новых файлов в диффе не видно.
    """
    a = _git(["add", "-A", "--", "."], cwd=wt, timeout=120)
    if a.returncode != 0:
        raise DevExecError("git add: " + (a.stderr or a.stdout).strip()[:300])
    # --binary (бой 31.10, прогон f5ad65c1): исполнитель добавил PNG-иконки,
    # дифф без --binary дал «Binary files differ» — автопатч не применим
    d = _git(["diff", "--cached", "--no-color", "--no-ext-diff", "--binary"],
             cwd=wt, timeout=180)
    if d.returncode != 0:
        raise DevExecError("git diff --cached: " + (d.stderr or d.stdout).strip()[:300])
    patch = d.stdout or ""
    if len(patch) > max_chars:
        raise DevExecError(f"патч {len(patch)} символов больше потолка {max_chars}")
    return patch


def cleanup(rid: str) -> None:
    wt = worktree_path(rid)
    if wt.exists():
        _git(["worktree", "remove", "--force", str(wt)], timeout=120)
    _git(["worktree", "prune"], timeout=60)


def cleanup_leftovers() -> int:
    """Worktree прошлых прогонов: перезапуск сервиса обрывает execute() до finally,
    поэтому каталоги убираем здесь (не в _ACTIVE — значит прогон уже не исполняется)."""
    root = worktree_root()
    if not root.exists():
        _git(["worktree", "prune"], timeout=60)
        return 0
    n = 0
    for p in root.iterdir():
        if p.is_dir() and p.name not in _ACTIVE:
            _git(["worktree", "remove", "--force", str(p)], timeout=120)
            n += 1
    _git(["worktree", "prune"], timeout=60)
    return n


def _cleanup_int_worktrees() -> None:
    """Одноразовые интеграционные worktree автопатча (ns-int-* в temp): если
    процесс умер посреди apply, каталог остаётся — убираем на старте. Текущий
    worktree НЕ трогаем: smoke-гейт автопатча сам идёт внутри ns-int-*, и
    удаление себя из-под себя убило бы интеграцию."""
    here = os.path.realpath(os.getcwd())
    r = _git(["worktree", "list", "--porcelain"], timeout=60)
    for block in (r.stdout or "").split("\n\n"):
        path = ""
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
        if "/ns-int-" not in path:
            continue
        rp = os.path.realpath(path)
        if rp == here or here.startswith(rp + os.sep):
            continue
        _git(["worktree", "remove", "--force", path], timeout=120)
    _git(["worktree", "prune"], timeout=60)


def requeue_interrupted() -> list[str]:
    """Прогоны, потерянные на перезапуске, — в очередь (три класса, все найдены в бою).

    A. running без патча — ход исполнителя оборван (случай 31.10 01:26).
    B. running С патчом — процесс умер МЕЖДУ finish и commit/push автопатча
       (E2E c2a639d2 01.11). На старте живых задач нет вовсе, поэтому любой
       running — сирота; патч цел и будет интегрирован без переисполнения.
    C. done/error с патчом без штампа 'integrated', финиш не старше 90 мин —
       интеграция не завершилась (смерть процесса посреди apply). Прогоны,
       интегрированные старым механизмом, опознаются по коммиту «devrun <rid>»
       в origin/main и штампуются молча; спасённые ранее вручную помечены
       «не интегрировано (эра до recovery)» — не воскрешаются.

    created_at/finished_at освежаются: ловцы expire_stale_runs (queued>60 мин
    по created_at) и expire_orphan_running (last_life = max(created_at,
    last_turn) >= cutoff) иначе убивают воскрешённый прогон мгновенно —
    так 9 recovery-прогонов легли в error 01.11, не успев исполниться.
    Окно 90 мин, а не сутки: вчерашняя задача, воскрешённая поверх уехавшего
    кода, — это мусорный повтор, а не recovery.
    """
    from . import db
    from .db import now_iso
    _cleanup_int_worktrees()
    d = db.get_db()
    ids: list[str] = []
    ts = now_iso()
    # A+B: любой running на старте — сирота (живых задач в новом процессе нет)
    rows = d.execute(
        "SELECT id, patch FROM runs WHERE session_id='devrun'"
        " AND status='running'").fetchall()
    for r in rows:
        has_patch = bool((r["patch"] or "").strip())
        d.execute(
            "UPDATE runs SET status='queued', created_at=?, finished_at=NULL, err=?"
            " WHERE id=?",
            (ts,
             "интеграция не завершилась при рестарте — повтор" if has_patch
             else "сервис перезапустился — прогон возвращён в очередь",
             r["id"]))
        ids.append(r["id"])
    # C: done/error с патчем, свежие, без штампа
    rows2 = d.execute(
        "SELECT id, patch FROM runs WHERE session_id='devrun'"
        " AND status IN ('done','error')"
        " AND patch IS NOT NULL AND patch<>''"
        " AND datetime(replace(replace(COALESCE(finished_at,''),'T',' '),'Z',''))"
        "     >= datetime('now','-90 minutes')"
        " AND (err IS NULL OR (err NOT LIKE 'integrated%'"
        "      AND err NOT LIKE 'не интегрировано%'"
        "      AND err NOT LIKE 'устарел при recovery%'"
        "      AND err NOT LIKE 'интеграция не завершилась%'))").fetchall()
    for r in rows2:
        rid12 = str(r["id"])[:12]
        g = _git(["log", "origin/main", "--grep", f"devrun {rid12}",
                  "--format=%h", "-1"], timeout=60)
        found = (g.stdout or "").strip().splitlines()[:1]
        if found:
            d.execute("UPDATE runs SET err=? WHERE id=?",
                      (f"integrated {found[0]} (коммит найден в origin/main)",
                       r["id"]))
            continue
        d.execute(
            "UPDATE runs SET status='queued', created_at=?, finished_at=NULL,"
            " err='интеграция не завершилась при рестарте — повтор' WHERE id=?",
            (ts, r["id"]))
        ids.append(r["id"])
    d.commit()
    if ids:
        log.info("возвращены в очередь оборванные прогоны: %s",
                 ", ".join(i[:8] for i in ids))
    return ids


def _specs() -> list[dict]:
    out = []
    for s in tools.openai_specs():
        if (s.get("function") or {}).get("name") in _EXCLUDED_TOOLS:
            continue
        out.append(s)
    return out


_ACTIVE: set[str] = set()


def busy() -> bool:
    return bool(_ACTIVE)


def release(rid: str) -> None:
    """Снять резерв, если атомарный pickup не удался (прогон забрал кто-то другой)."""
    _ACTIVE.discard(str(rid)[:12])


def next_queued() -> dict | None:
    """Ближайший незабранный прогон; None, если исполнитель занят или очередь пуста.

    Резервирует прогон в _ACTIVE сразу, чтобы следующий тик цикла не взял второй
    до старта первого (правка и smoke параллельно не должны идти).
    """
    if _ACTIVE:
        return None
    cleanup_leftovers()
    rows = devruns.queued_devruns(limit=1)
    if not rows:
        return None
    row = rows[0]
    if not (row.get("task") or "").strip():
        return None
    _ACTIVE.add(str(row["id"])[:12])
    return row


def _finish_error(rid: str, err: str) -> None:
    try:
        devruns.finish(rid, "error", "", err[:400])
    except Exception as e:  # noqa: BLE001 — статус важнее точности ошибки
        log.warning("devrun %s: не записал error: %r", str(rid)[:8], e)


def _mark_not_integrated(rid: str, reason: str) -> None:
    """Снять «done», если патч не дошёл до origin/main.

    `devruns.finish(status='done')` пишется на готовом патче, ДО автопатча,
    поэтому без этой правки прогон оставался «выполненным» при провалившемся
    smoke, грязном дереве и push-гонке. Владелец видел закрытую задачу, а main
    оставался без правки.
    """
    from . import db
    from .db import now_iso
    d = db.get_db()
    d.execute("UPDATE runs SET status='error', finished_at=?, err=? WHERE id=?",
              (now_iso(), "не дошёл до main: " + reason[:300], str(rid)[:12]))
    d.commit()


async def execute(rid: str, task: str) -> dict:
    """Один прогон доработки: worktree → ход агента → патч → автопатч.

    Не бросает исключений наружу: всё, что пошло не так, уходит в статус прогона
    и в отчёт — цикл исполнителя и сервер должны жить дальше.

    Авто-rebase (слово владельца 2026-11-01): если autopatch не смог положить
    патч на свежий origin/main (stale — параллельная доработка уехала вперёд),
    прогон переисполняется заново на новой базе (devrun.stale_retries, по
    умолчанию 1 переисполнение) — правка делается поверх АКТУАЛЬНОГО кода,
    а не затирает чужую.

    Recovery после рестарта: прогон, возвращённый в очередь с готовым патчом
    (процесс умер между devruns.finish и commit — бой c2a639d2 01.11),
    интегрирует СОХРАНЁННЫЙ патч без переисполнения; при устаревшей базе
    следующая попытка переисполняет ход как обычно. Успех штампуется в
    runs.err как 'integrated <sha>' — стартовый recovery по этому штампу
    отличает интегрированные прогоны.
    """
    dc = load_config().devrun
    short = str(rid)[:12]
    rep: dict = {"rid": short, "ok": False, "applied": False, "committed": False,
                 "pushed": False, "restart": False, "files": [], "patch_chars": 0,
                 "smoke": "", "error": "", "reexec": 0, "rebased": 0,
                 "skip_push": False}
    _ACTIVE.add(short)
    wt: Path | None = None
    stale_retries = max(0, int(getattr(dc, "stale_retries", 1)))
    log.info("devrun %s: исполняю на сервере (git %s)", short, GIT)
    try:
        devruns.set_report(short, "progress")
    except Exception as e:  # noqa: BLE001 — отчёт не роняет прогон
        log.warning("devrun %s: report=progress не записан: %r", short, e)
    try:
        row0 = await asyncio.to_thread(devruns.get_devrun, short) or {}
        saved_patch = (row0.get("patch") or "").strip()
        recovery = bool(saved_patch)   # воскрешённый прогон: без переисполнения чужой задачи
        for attempt in range(stale_retries + 1):
            summary = ""
            if saved_patch and attempt == 0:
                patch = saved_patch
                summary = (row0.get("result_text") or "").strip()
                rep["patch_chars"] = len(patch)
                log.info("devrun %s: интегрирую сохранённый патч (recovery после рестарта)",
                         short)
            else:
                if wt is not None:
                    await asyncio.to_thread(cleanup, short)
                    wt = None
                wt, base = await asyncio.to_thread(prepare, short)
                rep["base_sha"] = base[:12]
                prompt = (f"Worktree прогона: {wt}\n"
                          f"База: {base[:12]} (origin/main боевого репозитория).\n\n"
                          f"Задача: {task}\n\n"
                          "Правь файлы только внутри worktree. Патч снимет сервер сам — "
                          "не коммить и не пушь.")
                if attempt > 0:
                    prompt += ("\n\nВнимание: прошлая правка не легла — на origin/main "
                               "появились параллельные изменения. База обновлена: перечитай "
                               "затронутые файлы заново и сделай правку поверх текущего кода.")
                    rep["reexec"] = attempt
                if str(row0.get("err") or "").startswith(
                        ("сервис перезапустился", "интеграция не завершилась")):
                    # recovery note (у dsh: «check its directory, git history and
                    # GitHub before repeating any outside action»): прогон уже
                    # прерывался рестартом, внешний мир мог измениться.
                    prompt += ("\n\nВнимание: этот прогон уже прерывался перезапуском "
                               "сервиса. Сначала посмотри git log origin/main по "
                               "затронутым файлам — часть работы могла уже попасть в "
                               "main; не повторяй вслепую то, что уже сделано.")
                ctx = {"session_id": None, "turn_no": 0, "reveal_secrets": [],
                       "allow_shell": True, "depth": 0}

                async def _child(tc: dict) -> str:
                    return await tools.execute_tool_call(tc, ctx=ctx)

                wall = max(60, int(dc.max_wall_minutes) * 60)
                res = await asyncio.wait_for(
                    pool.ask(dc.exec_class, prompt, system=EXEC_SYSTEM, tools=_specs(),
                             tool_executor=_child, max_tool_iters=int(dc.exec_max_iters),
                             turn_budget=int(dc.exec_budget) or None,
                             turn_kind="devrun", user="devexec", role="worker"),
                    timeout=wall)
                summary = (res.get("text") or "").strip()
                if not res.get("ok"):
                    raise DevExecError("ход исполнителя не удался: "
                                       + str(res.get("err") or "?")[:200])
                patch = await asyncio.to_thread(extract_patch, wt, int(dc.patch_max_chars))
                rep["patch_chars"] = len(patch)
                if not patch.strip():
                    await asyncio.to_thread(devruns.finish, short, "error",
                                            summary[:4000] or "пустой ответ исполнителя", "", "")
                    rep["error"] = ("исполнитель не оставил правок (пустой патч)"
                                    " — возможно, задачу уже закрыл предыдущий прогон")
                    return rep
                ph = hashlib.sha256(patch.encode("utf-8")).hexdigest()[:16]
                await asyncio.to_thread(devruns.finish, short, "done", summary[:4000], patch, ph)
            from . import autopatch
            ap = await asyncio.to_thread(autopatch.apply, short, patch, summary[:4000])
            if ap.get("stale") and recovery:
                # recovery чужой СТАРОЙ задачи: переисполнять агента поверх уехавшего
                # кода — риск мусорных правок. Честно закрываем, чат уведомится.
                # git-деталь обязательна: случай 01.11 16:04 (apply в сервисе отказал,
                # а ручной повтор того же патча лёг cleanly) без неё необъясним.
                detail = str(ap.get("error") or "")[:300]
                msg = ("сохранённый патч устарел к текущему origin/main — прогон не "
                       "воскрешён (перезапроси доработку в чате, если ещё нужна). "
                       f"Деталь: {detail}")
                try:
                    from . import db as _db
                    from .db import now_iso as _now
                    d = _db.get_db()
                    d.execute("UPDATE runs SET status='error', finished_at=?, err=?"
                              " WHERE id=?", (_now(), "устарел при recovery: " + msg, short))
                    d.commit()
                except Exception as e:  # noqa: BLE001
                    log.warning("devrun %s: статус recovery-stale не записан: %r", short, e)
                rep["error"] = msg
                return rep
            if ap.get("stale") and attempt < stale_retries:
                log.info("devrun %s: устаревшая база (параллельные правки) — "
                         "переисполняю на свежем origin/main (попытка %d)",
                         short, attempt + 2)
                saved_patch = ""   # сохранённый патч устарел — дальше заново
                continue
            if ap.get("pushed") or ap.get("already"):
                stamp = (f"integrated {ap.get('commit_sha')}" if ap.get("pushed")
                         else "integrated (уже в origin/main)")
                if ap.get("verified"):
                    stamp += " (подтверждено в origin/main)"
                try:
                    from . import db
                    d = db.get_db()
                    d.execute("UPDATE runs SET err=? WHERE id=?", (stamp, short))
                    d.commit()
                except Exception as e:  # noqa: BLE001 — штамп не роняет отчёт
                    log.warning("devrun %s: штамп integrated не записан: %r", short, e)
            integrated = bool(ap.get("pushed") or ap.get("already")) and \
                bool(ap.get("verified"))
            if not integrated:
                # «done» был записан на готовом патче, ДО интеграции. Если патч не
                # лёг (smoke, грязное дерево, push-гонка) или не подтверждён в
                # origin/main, прогон обязан стать error: иначе задача выглядит
                # закрытой, а правки в main нет — именно это судья 02.11 назвал
                # «результат в транскрипте не подтверждён» (12 чатов из 15).
                reason = str(ap.get("error") or "автопатч не интегрировал патч")[:300]
                await asyncio.to_thread(_mark_not_integrated, short, reason)
                try:
                    devruns.set_report(short, "failed")
                except Exception as e:  # noqa: BLE001
                    log.warning("devrun %s: report=failed не записан: %r", short, e)
                rep.update({"ok": False, "applied": bool(ap.get("applied")),
                            "committed": bool(ap.get("committed")),
                            "pushed": bool(ap.get("pushed")), "restart": False,
                            "verified": False, "files": ap.get("files") or [],
                            "smoke": ap.get("smoke") or "",
                            "error": "не дошёл до main: " + reason,
                            "rebased": int(ap.get("rebased") or 0)})
                return rep
            try:
                devruns.set_report(short, "ready")
            except Exception as e:  # noqa: BLE001
                log.warning("devrun %s: report=ready не записан: %r", short, e)
            if ap.get("already"):
                rep["skip_push"] = True   # recovery: чат-заметка да, бомбить телефон — нет
            rep.update({"ok": True, "applied": True, "verified": True,
                        "committed": bool(ap.get("committed")),
                        "pushed": bool(ap.get("pushed")),
                        "restart": bool(ap.get("restart")),
                        "commit_sha": ap.get("commit_sha") or "",
                        "already": bool(ap.get("already")),
                        "files": ap.get("files") or [], "smoke": ap.get("smoke") or "",
                        "error": ap.get("error") or "",
                        "rebased": int(ap.get("rebased") or 0)})
            return rep
        rep["error"] = "цикл переисполнений завершился без результата"
        return rep
    except asyncio.TimeoutError:
        rep["error"] = f"стена прогона {dc.max_wall_minutes} мин"
        await asyncio.to_thread(_finish_error, short, rep["error"])
        return rep
    except Exception as e:  # noqa: BLE001 — прогон не должен ронять сервис
        rep["error"] = f"{type(e).__name__}: {e}"[:400]
        await asyncio.to_thread(_finish_error, short, rep["error"])
        return rep
    finally:
        _ACTIVE.discard(short)
        if wt is not None:
            try:
                await asyncio.to_thread(cleanup, short)
            except Exception as e:  # noqa: BLE001
                log.warning("devrun %s: worktree не убран: %r", short, e)
