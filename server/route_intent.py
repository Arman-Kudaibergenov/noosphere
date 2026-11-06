"""route_intent.py — единая точка входа: классификатор запроса (батч 6).

Три класса: chat (без инструментов) | agent (инструменты в диалоге) | run
(фоновый прогон через /v1/runs). Дефолт = run, если из текста строится задача;
chat — только по явным маркерам владельца («обсудим», «что думаешь»…).

Порядок решения: 1) chat-regex (явный маркер побеждает всё) → 2) run-regex →
3) LLM (pool.ask task_class="route", базовый тир, без эскалации) → при смерти
каналов честный fallback run с err. Проект решают ключевые слова ДО LLM
(бесплатный сигнал); LLM может назвать новый — projects.find_or_create.

Каждая классификация — строка route_log (модель/тир/токены/латентность);
правка пользователя — override=1, самый ценный сигнал для ночного анализа.
"""
from __future__ import annotations

import json
import re

from . import pool, projects
from .config import load_config
from .db import get_db, now_iso, today_local

# Явные маркеры «поговорить, ничего не делая».
# Порядок префильтра (G3, правило владельца «ран приоритетнее, если иначе явно
# не запрашиваю»): сначала RUN, потом CHAT — «почему не работает X, почини»
# уходит в run. Чистый вопрос («почему…», «что думаешь») run-регекс не
# матчит, поэтому chat-маркер по-прежнему побеждает на чистых вопросах.
PREFILTER_CHAT = re.compile(
    r"(привет|здравствуй|добрый (день|вечер|утро)|обсудим|давай поговорим|"
    r"что (ты )?думаешь|просто вопрос|не делай|ничего не делай|расскажи|объясни|"
    r"почему|как работает|что такое|посоветуй|мнение)", re.IGNORECASE)

# Маркеры задачи: из такого текста строится прогон.
PREFILTER_RUN = re.compile(
    r"(сделай|собери|почини|настрой|разверни|реализуй|напиши код|проверь|выполни|"
    r"запусти|обнови|задеплой|создай)", re.IGNORECASE)

KINDS = ("chat", "agent", "run")

_SYSTEM = (
    "Ты — маршрутизатор запросов личного рабочего места ИИ-агентов. "
    "Определи тип запроса и проект. Ответ — СТРОГО один JSON-объект без пояснений: "
    '{"kind": "chat|agent|run", "project": "<имя проекта или пусто>", '
    '"title": "<заголовок сессии до 60 символов>", "confidence": 0.0-1.0}. '
    "kind: chat — разговор/вопрос без действий; agent — задача, требующая "
    "инструментов, но в диалоге; run — фоновая задача-прогон (сделай/почини/"
    "разверни…). Сомневаешься между agent и run — выбирай run. "
    "project — одно из имён списка проектов или новое короткое имя; если непонятно — пусто.")


def _log(session_id: str | None, snippet: str, kind: str, project: dict | None,
         title: str, confidence: float, prefilter: bool, res: dict | None,
         err: str = "") -> int:
    """Строка route_log — пишется всегда, даже при провале LLM."""
    usage = (res or {}).get("usage") or {}
    db = get_db()
    cur = db.execute(
        "INSERT INTO route_log (ts, day, session_id, snippet, kind, project_id,"
        " project_name, title, confidence, prefilter, model, tier, prompt_tokens,"
        " completion_tokens, ms, err) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now_iso(), today_local(), session_id, snippet[:500], kind,
         project["id"] if project else None, project["name"] if project else None,
         title, confidence, 1 if prefilter else 0,
         (res or {}).get("model", ""), (res or {}).get("tier", ""),
         usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
         (res or {}).get("ms", 0), err))
    db.commit()
    return int(cur.lastrowid)


def _parse_llm_json(text: str) -> dict | None:
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("{"):s.rfind("}") + 1] if "{" in s else s
    try:
        data = json.loads(s)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def classify(prompt: str, session_titles: list[str] | None = None,
                   session_id: str | None = None) -> dict:
    """Классификация одного запроса. Не бросает исключений наружу: провал LLM
    → fallback run (каналы мертвы — не повод терять запрос), err в route_log."""
    cfg = load_config().route
    snippet = (prompt or "").strip()
    project = projects.match_by_keywords(snippet)  # бесплатный сигнал до LLM

    # 1-2. Префильтр (G3): сначала RUN-маркеры, потом CHAT (ран приоритетнее;
    # чистый вопрос run-регекс не матчит, поэтому явный chat всё ещё побеждает).
    if cfg.prefilter:
        kind = None
        if PREFILTER_RUN.search(snippet):
            kind = "run"
        elif PREFILTER_CHAT.search(snippet):
            kind = "chat"
        if kind:
            log_id = _log(session_id, snippet, kind, project, snippet[:60],
                          0.95, True, None)
            return {"log_id": log_id, "kind": kind, "project": project,
                    "title": snippet[:60], "confidence": 0.95, "prefilter": True,
                    "threshold": cfg.confidence_threshold,
                    "model": "", "tier": "", "ms": 0, "err": ""}

    # 3. LLM-классификация (дешёвый тир, JSON-контракт, без эскалации)
    plist = "\n".join(f"- {p['name']} (ключи: {p['keywords'] or 'нет'})"
                      for p in projects.list_projects()) or "- (проектов пока нет)"
    hist = "\n".join(f"- {t}" for t in (session_titles or [])[:10] if t)
    user_msg = (f"Проекты:\n{plist}\n\nПоследние сессии:\n{hist or '- (нет)'}\n\n"
                f"Запрос: {snippet[:1500]}")
    res = await pool.ask("route", user_msg, system=_SYSTEM, tier=cfg.tier,
                         max_tokens=cfg.max_tokens, check_json=True, min_chars=2,
                         force_no_escalate=True, role="route",
                         session_id=session_id, user="route")
    data = _parse_llm_json(res.get("text", "")) if res.get("ok") else None
    if data is None:
        # каналы мёртвы или ответ не JSON — классификация НЕ падает
        err = res.get("err") or "не разобрался как JSON"
        log_id = _log(session_id, snippet, "run", project, snippet[:60], 0.0,
                      False, res, err)
        return {"log_id": log_id, "kind": "run", "project": project,
                "title": snippet[:60], "confidence": 0.0, "prefilter": False,
                "threshold": cfg.confidence_threshold,
                "model": res.get("model", ""), "tier": res.get("tier", ""),
                "ms": res.get("ms", 0), "err": err}

    kind = str(data.get("kind") or "").lower()
    if kind not in KINDS:
        kind = "run"
    try:
        confidence = min(1.0, max(0.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    pname = str(data.get("project") or "").strip()
    resolve_err = ""
    if pname and projects.get_by_name(pname) is None:
        if not cfg.auto_projects:
            project = project or None  # автосоздание выключено — остаётся keyword-match
        elif projects.auto_count() >= cfg.auto_projects_max:
            # потолок auto-проектов (G3): сверх него — свалка в «Личное» +
            # warning-алерт раз в сутки (список в промпте классификатора не растёт)
            project = projects.get_by_name("Личное")
            _warn_cap_once_a_day(cfg.auto_projects_max)
        else:
            try:
                project = projects.find_or_create(pname)
            except Exception as e:  # IntegrityError при параллельном дубле и т.п.
                resolve_err = f"проект не создан: {e!r}"[:200]
                project = projects.get_by_name(pname)  # гонка: успел создать сосед
    elif pname:
        try:
            project = projects.find_or_create(pname)  # существующий — добивка не нужна
        except Exception as e:
            resolve_err = f"проект не создан: {e!r}"[:200]
            project = projects.get_by_name(pname)
    title = str(data.get("title") or snippet[:60]).strip()[:80]
    log_id = _log(session_id, snippet, kind, project, title, confidence, False, res,
                  resolve_err)
    return {"log_id": log_id, "kind": kind, "project": project, "title": title,
            "confidence": confidence, "prefilter": False,
            "threshold": cfg.confidence_threshold,
            "model": res.get("model", ""), "tier": res.get("tier", ""),
            "ms": res.get("ms", 0), "err": ""}


def _warn_cap_once_a_day(cap: int) -> None:
    """Warning о достигнутом потолке auto-проектов — раз в сутки (не спамить
    на каждой классификации)."""
    db = get_db()
    key = "auto_projects_cap_warned"
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row and row[0] == today_local():
        return
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
               (key, today_local()))
    db.commit()
    try:
        from . import alerts
        alerts.send("warning", "route",
                    f"потолок auto-проектов ({cap}) достигнут — новые имена "
                    "классификатора складываются в «Личное»; разберись со списком")
    except Exception:
        pass


def list_log(limit: int = 50, day: str | None = None) -> list[dict]:
    """Чтение route_log наружу (G3): GET /v1/route_log?limit=&day=."""
    db = get_db()
    if day:
        rows = db.execute(
            "SELECT * FROM route_log WHERE day=? ORDER BY id DESC LIMIT ?",
            (day, limit)).fetchall()
    else:
        rows = db.execute("SELECT * FROM route_log ORDER BY id DESC LIMIT ?",
                          (limit,)).fetchall()
    return [dict(r) for r in rows]


def override(log_id: int, kind: str | None = None,
             project_id: int | None = None) -> bool:
    """Правка пользователя — самый ценный сигнал (анализируется ночью)."""
    db = get_db()
    if db.execute("SELECT 1 FROM route_log WHERE id=?", (log_id,)).fetchone() is None:
        return False
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind должен быть одним из {KINDS}")
    db.execute("UPDATE route_log SET override=1, override_kind=?,"
               " override_project_id=? WHERE id=?", (kind, project_id, log_id))
    db.commit()
    return True
