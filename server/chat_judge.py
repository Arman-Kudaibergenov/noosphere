"""Процесс-судья: финальный статус каждого закрытого чата (дизайн 2026-10-12, §2).

Ночной headless-прогон: вызов через `pool.ask` с `turn_kind="judge"` (цена аудита
видна в burn отдельной строкой), класс задачи `audit` → deep, `force_no_escalate` —
судья не тянет лестницу; инструментов и shell у него нет, то есть исполняющая
поверхность не растёт. Отдельные ходы владелец не размечает: его золото —
override маршрута и метка на чат целиком (`owner_marks`: ok = принято,
notit = не то). Действующая метка перевешивает вывод судьи о приёмке
(`verdicts.owner_effect` → `_clamp_by_mark`), а в промт уходит уликой.

Если судья недоступен или ответил невалидно, пишется proxy-вердикт правил с явной
пометкой в reason — отчёт не притворяется оцененным и не молчит.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import db, pool, transcript, verdicts

JUDGE_PROMPT_VERSION = 2
STATUSES = ("success", "partial", "fail", "unresolved")
META_KEY = "audit_judge_day"

# Жёсткий вердикт — основа (status), мягкий — рядом для вариативности (soft_status):
# владелец хочет видеть оба, а не один усреднённый (решение 2026-10-12).
DUAL_ADDENDUM = (
    " Верни ОБА вердикта: \"status\" — жёсткий (задача решена и принята целиком;"
    " tech-провалы и брошенные петли = fail), и \"soft_status\" — мягкий (пользователь"
    " получил пользу или заметный прогресс, даже если задача закрыта не полностью;"
    " soft не может быть выше success и ниже unresolved). Плюс \"reason\" для жёсткого"
    " и \"soft_reason\" для мягкого, по 1-2 предложения.")

# BACKLOG-15 п.5: действующий промпт судьи берётся из judge_prompts, а там лежит
# версия 1 (416 зн, 12.10) — она побеждает встроенную v2 и НЕ требует ни JSON, ни
# confidence. Поэтому confidence=0.0 в 44 из 47 строк: судья его просто не просили
# вернуть. Контракт формата добавлен сюда, а не в judge_prompts: утверждённый
# владельцем текст промпта не переписываем.
FORMAT_ADDENDUM = (
    " Формат ответа обязателен: ровно один JSON-объект и ничего вокруг него —"
    ' {"status": "success|partial|fail|unresolved", "reason": "1-2 предложения",'
    ' "evidence_turns": [номера ходов], "confidence": число от 0.0 до 1.0,'
    ' "soft_status": "success|partial|fail|unresolved", "soft_reason": "1-2 предложения"}.'
    " В confidence пиши числом, насколько транскрипт и признаки позволяют вынести"
    " вердикт: 0.9 и выше — исход виден из данных однозначно; 0.5-0.8 — часть"
    " цепочки не видна; ниже 0.5 — данных мало, и тогда status обязан быть"
    " unresolved. Не оставляй поле пустым: ноль означает «данных нет вовсе».")

JUDGE_SYSTEM = (
    "Ты — аудитор успешности рабочих чатов личного рабочего места. Тебе дают сжатый"
    " транскрипт чата и детерминированные признаки (коды возврата инструментов, обрывы,"
    " контент-фильтр, переделки, протухшие заявки). Оцени, РЕШЕНА ли задача"
    " пользователя в этом чате, а не вежливость ответа. Отвечай ТОЛЬКО JSON-объектом:"
    ' {"status": "success|partial|fail|unresolved", "reason": "1-2 предложения",'
    ' "evidence_turns": [номера ходов], "confidence": 0.0-1.0}.'
    " success — задача решена и принята; partial — решена частично или с переделками;"
    " fail — не решена или решена неверно; unresolved — данных недостаточно."
    " Не выдумывай факты вне транскрипта и не предлагай действий.")

# Метка владельца — единственное свидетельство приёмки, которое не выводится из
# транскрипта. До 02.11 судья ставил partial за «приёмка не подтверждена» там, где
# владелец результат взял: 21 partial за 30 дней при 0 строк в owner_marks.
# Добавлен кодом, а не в judge_prompts: утверждённый владельцем текст не переписываем.
OWNER_MARK_ADDENDUM = (
    " В признаках есть метка владельца — это факт приёмки, а не твоё предположение:"
    " «принято» означает, что владелец взял результат (не занижай статус из-за"
    " неподтверждённой приёмки), «не то» означает, что результат неверен (статус не"
    " выше fail). Метка с пометкой «поставлена до последних ходов» описывает часть"
    " чата, а не весь чат — учитывай её как улику и суди по ходу после метки.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_prompt(tr_text: str, stats: dict) -> str:
    mark = OWNER_MARK_ADDENDUM if stats.get("owner_mark") else ""
    return (f"Чат: ходов={stats['turns']}, вызовов модели={stats['attempts']},"
            f" вызовов инструментов={stats['tools']}.\n"
            f"Транскрипт и признаки:\n{tr_text}\n"
            "Дай финальный статус чата строго в требуемом JSON."
            + DUAL_ADDENDUM + FORMAT_ADDENDUM + mark)


def parse_verdict(text: str) -> dict | None:
    """Разбор ответа судьи: чисто для selftest и для боевого пути одинаково."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    status = str(data.get("status") or "").strip().lower()
    if status not in STATUSES:
        return None
    # «судья вернул 0» и «судья не вернул число» — разные факты: раньше оба
    # писались нулём, поэтому confidence=0.0 во всех строках не отличал
    # «неуверен» от «поле не запрошено промптом». NULL = числа не было (п.5).
    raw_conf = data.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(raw_conf))) if raw_conf is not None else None
    except (TypeError, ValueError):
        confidence = None
    turns = data.get("evidence_turns") or []
    if not isinstance(turns, list):
        turns = []
    soft = str(data.get("soft_status") or "").strip().lower()
    if soft not in STATUSES:
        soft = None
    return {"status": status, "reason": str(data.get("reason") or "")[:400],
            "soft_status": soft, "soft_reason": str(data.get("soft_reason") or "")[:400],
            "evidence_turns": [int(t) for t in turns if str(t).lstrip("-").isdigit()][:8],
            "confidence": confidence}


def _write(session_id: str, status: str, reason: str, evidence: str,
           confidence: float | None,
           model: str, tier: str, tokens: int, rules_agree: int | None, proxy: str,
           prompt_version: int = JUDGE_PROMPT_VERSION, mode: str = "nightly",
           soft_status: str | None = None, soft_reason: str = "") -> None:
    c = db.get_db()
    c.execute("INSERT OR REPLACE INTO chat_verdict(session_id, status, reason, evidence,"
              " confidence, judge_model, judge_tier, tokens, rules_agree,"
              " judge_prompt_version, rules_proxy, mode, soft_status, soft_reason,"
              " ts, closed_at)"
              " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (session_id, status, reason, evidence, confidence, model, tier, tokens,
               rules_agree, prompt_version, proxy, mode, soft_status, soft_reason,
               _now(), _now()))
    c.commit()


def _clamp_by_mark(parsed: dict, stats: dict) -> dict:
    """Действующая метка владельца перевешивает вывод судьи о приёмке.

    Правка детерминированная и видна в reason: вердикт судьи сохраняется в тексте,
    поэтому «что именно перебито» восстанавливается без догадок. Жёсткий и мягкий
    статусы приводятся к метке оба — иначе счётчик вариативности «жёсткий≠мягкий»
    рос бы там, где вопрос уже закрыл владелец.
    """
    eff = (stats or {}).get("owner_effect") or {}
    want = eff.get("status")
    if not want:
        return parsed
    soft = parsed.get("soft_status")
    if parsed.get("status") == want:
        # судья сам сказал то же, что владелец: его текст не переписывается, но
        # мягкий статус приводится к метке (бой 02.11: обе метки «не то» легли на
        # hard=fail/soft=partial, и отчёт продолжил считать чат вариативным)
        if not soft or soft == want:
            return parsed
        out = dict(parsed)
        out["soft_status"] = want
        out["soft_reason"] = (f"{parsed.get('soft_reason') or ''}"
                              " [мягкий статус приведён к метке владельца]")[:400]
        return out
    word = verdicts.MARK_WORD.get(eff.get("mark"), eff.get("mark"))
    note = f"метка владельца «{word}» {eff.get('ts') or ''}".strip()
    out = dict(parsed)
    out["status"] = want
    out["soft_status"] = want
    out["reason"] = (f"{note}: {want} вместо {parsed.get('status')}."
                     f" Вердикт судьи: {parsed.get('reason') or ''}")[:400]
    return out


def judge_prompt() -> tuple[str, int]:
    """Действующий промпт судьи: строка judge_prompts версии НЕ НИЖЕ кодовой.

    BACKLOG-15 п.20: раньше побеждала просто последняя строка таблицы —
    устаревшая версия 1 (12.10, 416 зн, без JSON-контракта и confidence)
    перекрывала встроенную v2, и confidence=0.0 в 44 из 47 вердиктов. Теперь
    JUDGE_PROMPT_VERSION — пол: строки ниже игнорируются, а контракт
    оптимизатора пишется ВЫШЕ пола (optimizer.apply_contract) и потому
    побеждает встроенный промпт — механика A2 сохранена.
    """
    c = db.get_db()
    try:
        row = c.execute("SELECT version, text FROM judge_prompts"
                        " WHERE version >= ? ORDER BY version DESC LIMIT 1",
                        (JUDGE_PROMPT_VERSION,)).fetchone()
    except Exception:
        row = None
    if row and (row["text"] or "").strip():
        return row["text"], int(row["version"])
    return JUDGE_SYSTEM, JUDGE_PROMPT_VERSION


def _session_meta(session_id: str) -> str:
    row = db.get_db().execute("SELECT meta FROM sessions WHERE id=?",
                              (session_id,)).fetchone()
    return row["meta"] if row else ""


async def judge_chat(session_id: str, mode: str = "nightly") -> dict:
    if verdicts.is_trial_meta(_session_meta(session_id)):
        # пробная сессия: вердикт не пишем вовсе — иначе синтетика снова попадает
        # в долю fail и в недельный отчёт (BACKLOG-15 п.5)
        return {"session_id": session_id, "skipped": "trial", "status": "skipped"}
    verdicts.recompute_session(session_id)  # признаки правил свежи на момент суда
    text, stats = transcript.condensed(session_id)
    out = {"session_id": session_id, "stats": stats}
    if not stats["turns"]:
        _write(session_id, "unresolved", "нет ходов для оценки", "[]", 0.0, "", "", 0,
               None, stats["proxy"], mode=mode)
        out.update(status="unresolved", tokens=0)
        return out
    system, prompt_version = judge_prompt()
    res = await pool.ask("audit", build_prompt(text, stats), system=system,
                         force_no_escalate=True, check_json=True, max_tokens=700,
                         turn_kind="judge", role="judge")
    parsed = parse_verdict(res.get("text") or "") if res else None
    usage = (res or {}).get("usage") or {}
    tokens = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    if not parsed:
        _write(session_id, stats["proxy"],
               "судья недоступен или ответил невалидно — proxy-вердикт правил",
               "[]", 0.0, (res or {}).get("model", ""), (res or {}).get("tier", ""),
               tokens, 1, stats["proxy"], prompt_version, mode=mode)
        out.update(status=stats["proxy"], tokens=tokens, proxy=True)
        return out
    parsed = _clamp_by_mark(parsed, stats)
    rules_agree = int(parsed["status"] == stats["proxy"])
    _write(session_id, parsed["status"], parsed["reason"],
           json.dumps(parsed["evidence_turns"]), parsed["confidence"],
           res.get("model", ""), res.get("tier", ""), tokens, rules_agree, stats["proxy"],
           prompt_version, mode=mode, soft_status=parsed["soft_status"],
           soft_reason=parsed["soft_reason"])
    out.update(status=parsed["status"], tokens=tokens, rules_agree=rules_agree,
               reason=parsed["reason"], soft_status=parsed["soft_status"])
    return out


def _needs_judge(session_id: str) -> bool:
    c = db.get_db()
    row = c.execute("SELECT ts, reason, tokens FROM chat_verdict WHERE session_id=?",
                    (session_id,)).fetchone()
    last = c.execute("SELECT MAX(ts) AS t FROM turns WHERE session_id=?",
                     (session_id,)).fetchone()
    if not row:
        return True
    if not row["tokens"] and (row["reason"] or "").startswith("судья недоступен"):
        # proxy-вердикт правил — не оценка (ночь 01.11: оба канала судьи легли,
        # все 5 вердиктов confidence=0). Судья ожил — чат будет пересужен.
        return True
    marked = c.execute("SELECT MAX(ts) AS t FROM owner_marks WHERE session_id=?",
                       (session_id,)).fetchone()
    if marked and marked["t"] and marked["t"] > row["ts"]:
        # метка владельца новее вердикта — вердикт вынесен без золотой метки.
        # Считается и строка `none` (снятие метки): чат обязан вернуться к оценке
        # по правилам и судье, а не остаться с прежним статусом.
        return True
    return bool(last and last["t"] and last["t"] > row["ts"])


async def nightly(cap: int = 15, idle_hours: float = 6.0) -> str | None:
    """Ночная фаза 1: финальные статусы закрытых чатов, suspect-first, с капом."""
    if not should_run_today():
        return None
    candidates = [sid for sid in verdicts.closed_chats(idle_hours, limit=cap * 3)
                  if _needs_judge(sid)][:cap]
    counts: dict[str, int] = {}
    tokens = 0
    disagreements = 0
    variability = 0
    skipped = 0
    proxy_n = 0
    for sid in candidates:
        try:
            out = await judge_chat(sid)
        except Exception as e:  # судья не критический контур: ночь не должна падать
            print(f"chat_judge: {sid} failed: {e!r}")
            continue
        if out.get("skipped"):
            skipped += 1
            continue
        counts[out.get("status", "unresolved")] = counts.get(out.get("status"), 0) + 1
        tokens += int(out.get("tokens") or 0)
        if out.get("proxy"):
            proxy_n += 1
        if out.get("rules_agree") == 0:
            disagreements += 1
        if out.get("soft_status") and out.get("soft_status") != out.get("status"):
            variability += 1
    judged = len(candidates) - skipped
    all_proxy = bool(judged) and proxy_n >= judged
    if not all_proxy:
        mark_run_today()
    # судья лёг целиком — день НЕ закрываем: _audit_loop повторит через час
    contract_line = "контрактов оптимизации за ночь нет"
    try:
        from . import optimizer
        _ids, contract_line = await optimizer.run_phase()
    except Exception as e:  # оптимизатор не критический контур: ночь не должна падать
        print(f"optimizer: {e!r}")
        contract_line = f"оптимизатор ошибся: {type(e).__name__}"
    if not candidates:
        return f"аудит чатов: за сутки закрытых чатов без вердикта нет; {contract_line}"
    parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "—"
    proxy_line = ""
    if all_proxy:
        proxy_line = (f", судья недоступен для {proxy_n} из {judged} — вердикты правил"
                      " (proxy), день не закрыт, повтор через час")
    elif proxy_n:
        proxy_line = f", судья недоступен для {proxy_n} — там вердикты правил (proxy)"
    return (f"аудит чатов за ночь: оценено {judged} ({parts}),"
            f" пробных пропущено {skipped},"
            f" токенов судьи {tokens}, расхождений с правилами {disagreements},"
            f" жёсткий≠мягкий {variability}{proxy_line}; {contract_line}")


def should_run_today() -> bool:
    c = db.get_db()
    row = c.execute("SELECT value FROM meta WHERE key=?", (META_KEY,)).fetchone()
    return not row or row["value"] != db.today_local()


def mark_run_today() -> None:
    c = db.get_db()
    c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
              (META_KEY, db.today_local()))
    c.commit()


DEEP_ADDENDUM = (" Это разбор выборки по явному запросу владельца (deep-dive): дай reason"
                " до трёх предложений и перечисли в evidence_turns все ходы, повлиявшие"
                " на вердикт.")


def resolve_deep_ids(session_ids: list[str] | None, status_filter: str,
                     limit: int = 5) -> list[str]:
    c = db.get_db()
    if session_ids:
        return [s for s in session_ids if s][:5]
    if status_filter in STATUSES:
        rows = c.execute(
            "SELECT session_id FROM chat_verdict WHERE status=? ORDER BY ts DESC LIMIT ?",
            (status_filter, min(limit, 5))).fetchall()
        return [r["session_id"] for r in rows]
    return []


async def deep_dive(session_ids: list[str] | None = None, status_filter: str = "",
                    tier: str = "max") -> list[dict]:
    """LLM-разбор выборки по явному запросу: дорогой тир, кап 5 чатов, без гейта ночи.

    Вердикт пишется с mode='deep' и заменяет ночной для этих чатов — владелец запросил
    разбор сам, поэтому дорогая модель здесь оправдана (решение 1 §8: max для расхождений
    и денежных/юридических чатов; deep-dive — тот же класс внимания).
    """
    outs: list[dict] = []
    explicit = bool(session_ids)
    for sid in resolve_deep_ids(session_ids, status_filter, 5):
        if not explicit and verdicts.is_trial_meta(_session_meta(sid)):
            continue    # владелец явно перечислил id — его воля; автовыборка без проб
        verdicts.recompute_session(sid)
        text, stats = transcript.condensed(sid)
        if not stats["turns"]:
            continue
        system, prompt_version = judge_prompt()
        res = await pool.ask("audit", build_prompt(text, stats) + DEEP_ADDENDUM,
                             system=system, tier=tier, force_no_escalate=True,
                             check_json=True, max_tokens=1200, turn_kind="judge")
        parsed = parse_verdict(res.get("text") or "") if res else None
        usage = (res or {}).get("usage") or {}
        tokens = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        if not parsed:
            _write(sid, stats["proxy"], "deep-dive: судья невалиден — proxy правил",
                   "[]", 0.0, (res or {}).get("model", ""), tier, tokens, 1,
                   stats["proxy"], prompt_version, mode="deep")
            outs.append({"session_id": sid, "status": stats["proxy"], "tokens": tokens,
                         "mode": "deep"})
            continue
        parsed = _clamp_by_mark(parsed, stats)
        rules_agree = int(parsed["status"] == stats["proxy"])
        _write(sid, parsed["status"], "deep-dive: " + parsed["reason"],
               json.dumps(parsed["evidence_turns"]), parsed["confidence"],
               res.get("model", ""), tier, tokens, rules_agree, stats["proxy"],
               prompt_version, mode="deep", soft_status=parsed["soft_status"],
               soft_reason=parsed["soft_reason"])
        outs.append({"session_id": sid, "status": parsed["status"],
                     "soft_status": parsed["soft_status"],
                     "reason": parsed["reason"], "tokens": tokens, "mode": "deep"})
    return outs


def stale_provisional(idle_hours: float = 6.0) -> int:
    """Сколько чатов ждут пересуда (новый ход после вердикта) — для вкладки «Аудит»."""
    c = db.get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=idle_hours)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = c.execute(
        "SELECT v.session_id FROM chat_verdict v JOIN turns t ON t.session_id=v.session_id"
        " GROUP BY v.session_id HAVING MAX(t.ts) > v.ts AND MAX(t.ts) <= ?",
        (cutoff,)).fetchall()
    return len(rows)
