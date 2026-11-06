"""processes.py — процессы разработки 1С «спека → код → судья» (v1.7, 2026-10-13).

Триада ролей (дизайн владельца): СПЕКА = alibaba/deepseek-v4-pro (подписка
Alibaba Token Plan; до 20.10 был zen/mimo — подписка opencode-go отменена),
ИСПОЛНИТЕЛЬ = alibaba/deepseek-v4.1-flash (подписка Token Plan; до 20.10 —
qwencloud payg), СУДЬЯ =
kimi-cli/k3.1-pro (подписка Kimi — судью держим на другом вендоре ради триангуляции;
проба 20.10 на реальном промпте: 64 с и валидный JSON; замеренные замены на плане,
если Kimi отвалится, — deepseek-v4-pro или qwen3.7-plus). Каждая стадия —
вызов pool.ask с force_channel/force_spec (лестница тиров не применяется),
класс телеметрии arch/code/review — burn и route_log остаются осмысленными.

Таблицы processes/process_stages (schema v19). Статусы процесса:
queued → running → done (ACCEPT) | needs_human (исчерпаны итерации правок)
| error | canceled. Артефакты стадий — полные тексты в process_stages.artifact.

Грабли (память проекта): думающие модели при малом max_tokens уходят в reasoning
и отдают пустой content — все стадии, кроме судьи, ретраятся (до 2) с удвоенным
max_tokens;
kimi ограничен 5-часовым окном — судья ОДИН вызов на стадию, без ретраев.
Deploy НЕ исполняется автоматически: deploy:true → стадия deploy_plan (план по
скилу dev-loop); исполнение — человеком/отдельным раном.
"""
from __future__ import annotations

import json
import uuid
import uuid

from . import pool, skills, tools
from .db import get_db, now_iso

TERMINAL_STATUSES = ("done", "needs_human", "error", "canceled")

# стадия → (канал, spec, max_tokens, task_class телеметрии, таймаут вызова;
# None — дефолт pool: 180с, у kind=zen 600с). Думающие стадии (спека, судья, код с
# reasoning) не укладываются в 180с — бой 13.10 и 20.10, ReadTimeout.
ROLES = {
    "spec": ("alibaba", {"model": "deepseek-v4-pro", "body": {}}, 8192, "arch", 600),
    "code": ("alibaba", {"model": "deepseek-v4.1-flash",
                         "body": {"reasoning_effort": "medium"}}, 16384, "code", 600),
    "judge": ("kimi-cli", {"model": "k3.1-pro", "body": {}}, 8192, "review", 600),
}

_SPEC_TOOLS = ("skill_list", "skill_load", "mcp_tools", "mcp_call")

SPEC_SYSTEM = (
    "Ты — архитектор 1С:Предприятие 8. По задаче дай СПЕКУ для исполнителя: "
    "затрагиваемые метаданные и модули (точные имена объектов), алгоритм по шагам, "
    "проверки и контроль, критерии приёмки. Стандарты 1С (имена, клиент-сервер, "
    "обработка ошибок) соблюдай. Если тема покрыта скилом — загрузи его "
    "(skill_list/skill_load) и опирайся на него; структуру кода конфигурации можно "
    "уточнить через MCP (mcp_tools/mcp_call). Не выдумывай метаданные: не уверен — "
    "пометь явно. Разведка короткая: не более 8 вызовов инструментов, затем пиши "
    "спеку. Ответ — только спека, без кода.")

CODE_SYSTEM = (
    "Ты — исполнитель 1С:Предприятие 8. По спеке напиши ПОЛНЫЙ код на встроенном "
    "языке (BSL) без заглушек и многоточий: каждая процедура/функция — целиком. "
    "Соблюдай стандарты 1С: области, директивы компиляции, НСтр для пользовательских "
    "строк, ОбщегоНазначения там, где он уместен. Используй только метаданные из "
    "спеки. Если спека противоречива — скажи об этом в начале ответа строкой "
    "«ЗАМЕЧАНИЕ: …», но код всё равно напиши.")

JUDGE_SYSTEM = (
    "Ты — строгий ревьюер кода 1С:Предприятие 8. Проверь код по спеке: "
    "компилируемость BSL (директивы, области, синтаксис), выдуманные метаданные и "
    "реквизиты, обращения к несуществующим объектам, безопасность запросов "
    "(параметры вместо конкатенации, нет запросов в цикле без нужды), соответствие "
    "критериям приёмки. Отвечай СТРОГО JSON-объектом без пояснений вокруг: "
    '{"verdict": "ACCEPT" | "NEEDS_FIXES", "issues": ["..."], "summary": "..."}. '
    "issues — конкретные, с указанием процедуры/строки; пустой массив при ACCEPT.")


def parse_judge_verdict(text: str) -> dict | None:
    """Разбор ответа судьи: первая JSON-скобка {…} (raw_decode терпит хвост).
    None — невалидно. verdict строго ACCEPT | NEEDS_FIXES."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    start = raw.find("{")
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(raw, start)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict") or "").strip().upper()
    if verdict not in ("ACCEPT", "NEEDS_FIXES"):
        return None
    issues = data.get("issues") or []
    if not isinstance(issues, list):
        issues = [str(issues)]
    return {"verdict": verdict, "issues": [str(i)[:500] for i in issues][:20],
            "summary": str(data.get("summary") or "")[:500]}


def create(kind: str, task: str, project: str | None, max_fix_iters: int = 2,
           deploy: bool = False) -> str:
    pid = uuid.uuid4().hex[:12]
    db = get_db()
    db.execute(
        "INSERT INTO processes (id, kind, task, project, status, deploy,"
        " max_fix_iters, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (pid, kind, task, project or "", "queued", int(deploy), max_fix_iters,
         now_iso()))
    db.commit()
    return pid


def mark(pid: str, status: str, error: str = "") -> None:
    db = get_db()
    finished = now_iso() if status in TERMINAL_STATUSES else None
    db.execute("UPDATE processes SET status=?, error=?,"
               " finished_at=COALESCE(?, finished_at) WHERE id=?",
               (status, error, finished, pid))
    db.commit()


def _add_stage(pid: str, seq: int, stage: str, role: str) -> int:
    cur = get_db().execute(
        "INSERT INTO process_stages (process_id, seq, stage, role, status,"
        " created_at) VALUES (?,?,?,?,'running',?)",
        (pid, seq, stage, role, now_iso()))
    get_db().commit()
    return int(cur.lastrowid)


def _finish_stage(stage_id: int, status: str, channel: str = "", model: str = "",
                  artifact: str = "", verdict: str = "", usage: dict | None = None,
                  ms: int = 0) -> None:
    db = get_db()
    db.execute("UPDATE process_stages SET status=?, channel=?, model=?, artifact=?,"
               " verdict=?, usage_json=?, ms=? WHERE id=?",
               (status, channel, model, artifact, verdict,
                json.dumps(usage or {}, ensure_ascii=False), ms, stage_id))
    db.commit()


def get(pid: str) -> dict | None:
    row = get_db().execute("SELECT * FROM processes WHERE id=?", (pid,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["stages"] = [dict(r) for r in get_db().execute(
        "SELECT seq, stage, role, channel, model, status, verdict, usage_json, ms,"
        " created_at, artifact FROM process_stages WHERE process_id=?"
        " ORDER BY seq", (pid,)).fetchall()]
    return out


def list_processes(limit: int = 50) -> list[dict]:
    rows = get_db().execute(
        "SELECT id, kind, substr(task, 1, 200) AS task, project, status, error,"
        " created_at, finished_at FROM processes ORDER BY created_at DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def cancel(pid: str) -> bool:
    """Мягкая отмена: между стадиями исполнитель проверяет статус в БД."""
    db = get_db()
    cur = db.execute("UPDATE processes SET status='canceled', finished_at=?"
                     " WHERE id=? AND status IN ('queued','running')",
                     (now_iso(), pid))
    db.commit()
    return cur.rowcount > 0


def _is_canceled(pid: str) -> bool:
    row = get_db().execute("SELECT status FROM processes WHERE id=?", (pid,)).fetchone()
    return bool(row and row["status"] == "canceled")


async def _stage(pid: str, seq: int, stage: str, prompt: str, system: str,
                 *, with_tools: bool = False) -> dict:
    """Одна стадия: принудительные канал+модель роли, телеметрия под классом
    роли (arch/code/review). Все стадии, кроме судьи: до 2 ретраев с удвоенным
    max_tokens, если ответ пуст/оборван (reasoning сжирает лимит).
    Судья (kimi) — один вызов, без ретраев: 5-часовое окно лимитировано."""
    channel, spec, max_tokens, role, timeout = ROLES[stage]
    stage_id = _add_stage(pid, seq, stage, role)
    kwargs: dict = {}
    if with_tools:
        specs = [s for s in tools.openai_specs()
                 if s["function"]["name"] in _SPEC_TOOLS]
        kwargs = {"tools": specs,
                  "tool_executor": lambda tc: tools.execute_tool_call(
                      tc, ctx={"session_id": f"proc:{pid}"}),
                  "max_tool_iters": 10}  # разведка скилов/MCP — 6 итераций мало (бой 13.10)
    res: dict = {}
    attempt_tokens = max_tokens
    retries = 0 if stage == "judge" else 2
    while True:
        res = await pool.ask(role, prompt, system=system, max_tokens=attempt_tokens,
                             force_channel=channel, force_spec=spec,
                             force_no_escalate=True, check_json=(stage == "judge"),
                             turn_kind="process", timeout=timeout, **kwargs)
        emptyish = not res.get("ok") and (
            "пустой" in (res.get("err") or "") or "обрыв" in (res.get("err") or ""))
        if not emptyish or retries <= 0:
            break
        retries -= 1
        attempt_tokens *= 2
    usage = res.get("usage") or {}
    ms = int(res.get("ms") or 0)
    if res.get("ok"):
        _finish_stage(stage_id, "done", channel, res.get("model", ""), res["text"],
                      usage=usage, ms=ms)
    else:
        _finish_stage(stage_id, "error", channel, res.get("model", ""),
                      res.get("text", ""), usage=usage, ms=ms)
    return res


def _spec_prompt(task: str, project: str) -> str:
    return (f"Задача разработки 1С:\n{task}\n\n"
            f"Проект/контур: {project or 'не указан'}.\n"
            "Составь спеку для исполнителя по правилам системного промпта.")


def _code_prompt(task: str, spec: str, issues: list[str] | None = None) -> str:
    p = f"Задача:\n{task}\n\nСпека архитектора:\n{spec}\n\nНапиши полный код BSL."
    if issues:
        p += ("\n\nПредыдущая версия НЕ прошла ревью. Исправь замечания:\n- "
              + "\n- ".join(issues))
    return p


def _judge_prompt(task: str, spec: str, code: str) -> str:
    return (f"Задача:\n{task}\n\nСпека:\n{spec[:6000]}\n\nКод на ревью:\n{code}\n\n"
            "Верни вердикт строго в требуемом JSON.")


async def run_dev_1c(pid: str) -> None:
    """Фоновый исполнитель процесса (вызывается asyncio-таской из main.py)."""
    row = get_db().execute("SELECT * FROM processes WHERE id=?", (pid,)).fetchone()
    if row is None:
        return
    task, project = row["task"], row["project"] or ""
    max_iters = int(row["max_fix_iters"] or 2)
    mark(pid, "running")
    seq = 0
    try:
        # 1. спека (с инструментами скилов/MCP)
        seq += 1
        system = SPEC_SYSTEM
        block = skills.auto_block()
        if block:
            system += "\n\n" + block
        res = await _stage(pid, seq, "spec", _spec_prompt(task, project), system,
                           with_tools=True)
        if not res.get("ok"):
            mark(pid, "error", f"spec: {res.get('err', '')[:300]}")
            return
        spec_text = res["text"]

        code_text, verdict = "", None
        issues: list[str] = []
        for it in range(max_iters + 1):
            if _is_canceled(pid):
                return
            seq += 1
            res = await _stage(pid, seq, "code",
                               _code_prompt(task, spec_text, issues or None),
                               CODE_SYSTEM)
            if not res.get("ok"):
                mark(pid, "error", f"code: {res.get('err', '')[:300]}")
                return
            code_text = res["text"]
            if _is_canceled(pid):
                return
            seq += 1
            res = await _stage(pid, seq, "judge",
                               _judge_prompt(task, spec_text, code_text),
                               JUDGE_SYSTEM)
            parsed = parse_judge_verdict(res.get("text") or "") if res.get("ok") else None
            if parsed is None:
                # судья недоступен/невалиден — без догадок: на человека
                _mark_judge_unknown(pid, seq, res)
                mark(pid, "needs_human",
                     f"судья не дал валидный вердикт: {(res.get('err') or '')[:200]}")
                return
            db = get_db()
            db.execute("UPDATE process_stages SET verdict=? WHERE process_id=? AND seq=?",
                       (parsed["verdict"], pid, seq))
            db.commit()
            verdict = parsed
            if parsed["verdict"] == "ACCEPT":
                break
            issues = parsed["issues"]
        if verdict is None or verdict["verdict"] != "ACCEPT":
            mark(pid, "needs_human",
                 f"исчерпаны итерации правок ({max_iters}), последние замечания: "
                 + "; ".join(issues[:3])[:300])
            return

        if row["deploy"]:
            if _is_canceled(pid):
                return
            seq += 1
            devloop = skills.load("dev-loop") or "(скил dev-loop не найден на диске)"
            plan_prompt = (
                f"Задача решена и принята ревьюером. Код:\n{code_text[:8000]}\n\n"
                f"Инструкция по выкладке (скил dev-loop):\n{devloop[:6000]}\n\n"
                "Составь план выкладки этого кода в базу: конкретные шаги, команды, "
                "проверки после применения. План исполнит человек или отдельный прогон "
                "после одобрения — пиши чётко и коротко.")
            res = await _stage(pid, seq, "deploy_plan", plan_prompt, CODE_SYSTEM)
            mark(pid, "done" if res.get("ok") else "needs_human",
                 "" if res.get("ok") else "deploy_plan не построен")
            return
        mark(pid, "done")
    except Exception as e:
        mark(pid, "error", repr(e)[:400])


def _mark_judge_unknown(pid: str, seq: int, res: dict) -> None:
    db = get_db()
    db.execute("UPDATE process_stages SET verdict='INVALID' WHERE process_id=? AND seq=?",
               (pid, seq))
    db.commit()
