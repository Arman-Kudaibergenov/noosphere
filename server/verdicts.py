"""Правила-признаки v1 аудита успешности: детерминированный слой, 0 LLM-токенов.

Роль (дизайн 2026-10-12, §3): признаки и evidence для процесса-судьи
(`chat_judge.py`), пре-фильтр и предохранитель: если судья недоступен или бюджет
ночи исчерпан, в отчёт идёт proxy-вердикт правил с явной пометкой. Вердикт чата
выносит судья; отдельные ходы владелец не размечает — его золотая метка ставится
на чат целиком (`owner_marks`: ok = принято, notit = не то) и перевешивает вывод
судьи и правил о приёмке.

Правила версионируются (`RULES_VERSION` пишется в каждую строку): смена правила не
переписывает историю, пересчёт кладёт строки новой версии рядом.

Границы хода: `turns` = попытки вызова, `session_turns` = ходы; попытки привязываются
к ходу окном [ts хода, ts следующего хода), tool_audit и approvals joining'ом по
session_id + тому же окну (у tool_audit есть свой turn_no — он первичен).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from . import db

RULES_VERSION = 3  # v3 (02.11): класс хода = доминантный по токенам, а не первая попытка
REWORK_MIN = 30
REWORK_MARKERS = ("не то", "заново", "ошибка", "почини", "вернись", "переделай",
                  "не работает", "снова")
SLA_MS_DEFAULT = 180_000
P90_WINDOW_DAYS = 7

# Метки владельца (приёмка): ok — результат принят, notit — результат неверен,
# none — метка снята. Действующая = последняя строка журнала owner_marks.
MARKS = ("ok", "notit", "none")
GOLD_MARKS = ("ok", "notit")
MARK_STATUS = {"ok": "success", "notit": "fail"}
MARK_WORD = {"ok": "принято", "notit": "не то", "none": "метка снята"}

_CONN = None  # инжекция для selftest: своя sqlite-коннекция вместо боевой


def set_conn(conn) -> None:
    global _CONN
    _CONN = conn


def _c():
    return _CONN if _CONN is not None else db.get_db()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def turn_window(c, session_id: str, turn_no: int) -> tuple[str, str | None]:
    row = c.execute("SELECT ts FROM session_turns WHERE session_id=? AND turn_no=?",
                    (session_id, turn_no)).fetchone()
    if not row:
        return "", None
    nxt = c.execute("SELECT ts FROM session_turns WHERE session_id=? AND turn_no>?"
                    " ORDER BY turn_no LIMIT 1", (session_id, turn_no)).fetchone()
    return row["ts"], (nxt["ts"] if nxt else None)


def turn_attempts(c, session_id: str, turn_no: int) -> list:
    start, end = turn_window(c, session_id, turn_no)
    if not start:
        return []
    sql = ("SELECT ts, task_class, tier_start, tier_used, ok, answered, finish, err,"
           " escalated, content_filter, ms, prompt_tokens, completion_tokens, cost_usd"
           " FROM turns WHERE session_id=? AND ts>=?")
    args: list = [session_id, start]
    if end:
        sql += " AND ts<?"
        args.append(end)
    return c.execute(sql + " ORDER BY ts", args).fetchall()


def turn_tool_rows(c, session_id: str, turn_no: int) -> list:
    return c.execute(
        "SELECT tool, rc, note, args_summary, out_len FROM tool_audit"
        " WHERE session_id=? AND turn_no=? ORDER BY id",
        (session_id, turn_no)).fetchall()


def approvals_in_window(c, session_id: str, turn_no: int) -> list:
    start, end = turn_window(c, session_id, turn_no)
    if not start:
        return []
    sql = "SELECT kind, status, summary FROM approvals WHERE session_id=? AND ts>=?"
    args: list = [session_id, start]
    if end:
        sql += " AND ts<?"
        args.append(end)
    return c.execute(sql, args).fetchall()


def _p90_turn_tokens(c, task_class: str) -> int:
    """p90 расхода ХОДА: сумма всех его попыток (агрегаты v21, строка turn_last=1).

    До v2 здесь был p90 ОДНОГО вызова, а `spent` в eval_turn суммировал все
    попытки хода (в среднем 12 вызовов модели на ход). Правило поэтому
    срабатывало почти всегда и переводило ход в «partial» при живых tech_ok и
    tool_ok: замер 02.11 — 105 из 105 partial за 7 дней, `spent` до 916 063
    против `p90(chat)=19 909`.
    """
    rows = c.execute(
        "SELECT turn_prompt_tokens + turn_completion_tokens AS t FROM turns"
        " WHERE task_class=? AND day >= date('now', ?) AND ok=1 AND turn_last=1",
        (task_class, f"-{P90_WINDOW_DAYS} days")).fetchall()
    vals = sorted(r["t"] for r in rows if r["t"])
    if len(vals) < 5:
        return 0
    return vals[int(len(vals) * 0.9)]


def _turn_class(attempts) -> str:
    """Класс хода = класс с наибольшим суммарным расходом токенов.

    До v3 класс брали по ПЕРВОЙ попытке, а это обычно классификатор
    (`task_class='route'`, ~1k токенов), тогда как основная работа хода идёт
    классом chat/code. p90 для route (~1154) применялся к расходу всего хода —
    отсюда evidence вида `spent=739312 p90_turn(route)=1154`: «дорого» там,
    где просто шёл обычный чат с инструментами.
    """
    tot: dict[str, int] = {}
    for a in attempts:
        k = a["task_class"] or "chat"
        tot[k] = tot.get(k, 0) + (a["prompt_tokens"] or 0) + (a["completion_tokens"] or 0)
    if not tot:
        return "chat"
    return max(tot.items(), key=lambda kv: kv[1])[0]


def eval_turn(session_id: str, turn_no: int, conn=None) -> dict:
    """Признаки TECH/TOOL/COST одного хода + именованные сигналы с evidence."""
    c = conn or _c()
    signals: list[dict] = []
    attempts = turn_attempts(c, session_id, turn_no)
    tools = turn_tool_rows(c, session_id, turn_no)
    approvals = approvals_in_window(c, session_id, turn_no)

    tech_ok = True
    for a in attempts:
        if (a["err"] or "").strip():
            tech_ok = False
            signals.append({"rule": "tech_err", "evidence": f"turns.err={a['err'][:120]}"})
        if a["finish"] == "length":
            tech_ok = False
            signals.append({"rule": "tech_length",
                            "evidence": f"finish=length tokens={a['completion_tokens']}"})
        if a["content_filter"]:
            tech_ok = False
            signals.append({"rule": "tech_filter", "evidence": "content_filter=1"})
        if a["ms"] > SLA_MS_DEFAULT:
            tech_ok = False
            signals.append({"rule": "tech_slow", "evidence": f"ms={a['ms']}"})
    if not attempts:
        signals.append({"rule": "tech_no_attempts", "evidence": "нет строк turns в окне хода"})

    from . import tools as tools_mod
    tool_ok = True
    for t in tools:
        if t["rc"] not in (0, None) and not tools_mod.is_benign_rc(
                t["tool"], t["rc"], t["args_summary"] or ""):
            # rc=1 у grep/rg — «совпадений нет», а не отказ (слово владельца 01.11)
            tool_ok = False
            signals.append({"rule": "tool_rc",
                            "evidence": f"{t['tool']} rc={t['rc']} {t['args_summary'] or ''}"[:160]})
        if (t["note"] or "").strip():
            tool_ok = False
            signals.append({"rule": "tool_note",
                            "evidence": f"{t['tool']} note={t['note']}"[:160]})
    for ap in approvals:
        if ap["status"] == "denied":
            tool_ok = False
            signals.append({"rule": "approval_denied",
                            "evidence": f"{ap['kind']}: {ap['summary']}"[:160]})

    cost_ok = True
    for a in attempts:
        if a["escalated"] and not a["ok"]:
            cost_ok = False
            signals.append({"rule": "cost_escalated_fail",
                            "evidence": f"{a['tier_start']}→{a['tier_used']} ok=0"})
    if attempts:
        cls = _turn_class(attempts)
        cap = _p90_turn_tokens(c, cls)
        spent = sum((a["prompt_tokens"] or 0) + (a["completion_tokens"] or 0) for a in attempts)
        if cap and spent > cap:
            cost_ok = False
            signals.append({"rule": "cost_p90",
                            "evidence": f"spent={spent} p90_turn({cls})={cap}"})

    if not tech_ok or not tool_ok:
        verdict = "fail"
    elif not cost_ok:
        verdict = "partial"
    else:
        verdict = "success"
    return {"session_id": session_id, "turn_no": turn_no, "tech_ok": tech_ok,
            "tool_ok": tool_ok, "cost_ok": cost_ok, "verdict": verdict,
            "signals": signals, "rules_version": RULES_VERSION}


def recompute_turn(session_id: str, turn_no: int, conn=None) -> dict:
    c = conn or _c()
    out = eval_turn(session_id, turn_no, c)
    c.execute("DELETE FROM turn_verdict WHERE session_id=? AND turn_no=? AND rules_version=?",
              (session_id, turn_no, RULES_VERSION))
    c.execute("INSERT INTO turn_verdict(session_id, turn_no, tech_ok, tool_ok, cost_ok,"
              " verdict, rules_version, ts) VALUES(?,?,?,?,?,?,?,?)",
              (session_id, turn_no, int(out["tech_ok"]), int(out["tool_ok"]),
               int(out["cost_ok"]), out["verdict"], RULES_VERSION,
               datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")))
    c.execute("DELETE FROM verdict_signals WHERE session_id=? AND turn_no=? AND rules_version=?",
              (session_id, turn_no, RULES_VERSION))
    for s in out["signals"]:
        c.execute("INSERT INTO verdict_signals(ts, session_id, turn_no, rule, evidence,"
                  " rules_version) VALUES(?,?,?,?,?,?)",
                  (datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                   session_id, turn_no, s["rule"], s["evidence"], RULES_VERSION))
    c.commit()
    return out


def recompute_session(session_id: str, conn=None) -> int:
    c = conn or _c()
    rows = c.execute("SELECT turn_no FROM session_turns WHERE session_id=? ORDER BY turn_no",
                     (session_id,)).fetchall()
    for r in rows:
        recompute_turn(session_id, r["turn_no"], c)
    return len(rows)


def recompute_days(days: int = 30, conn=None) -> int:
    c = conn or _c()
    rows = c.execute(
        "SELECT DISTINCT session_id FROM session_turns WHERE ts >= ?",
        ((datetime.now(timezone.utc) - timedelta(days=days))
         .isoformat(timespec="seconds").replace("+00:00", "Z"),)).fetchall()
    for r in rows:
        recompute_session(r["session_id"], c)
    return len(rows)


def _overlap(a: str, b: str) -> float:
    ta, tb = set(re.findall(r"[\wа-яё]{4,}", a.lower())), set(re.findall(r"[\wа-яё]{4,}", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def chat_signals(session_id: str, conn=None) -> list[dict]:
    """Переделки и незакрытые петли чата — сигналы, а не вердикты."""
    c = conn or _c()
    out: list[dict] = []
    turns = c.execute(
        "SELECT turn_no, ts, snapshot FROM session_turns WHERE session_id=? ORDER BY turn_no",
        (session_id,)).fetchall()
    for prev, nxt in zip(turns, turns[1:]):
        gap = _parse(nxt["ts"]) - _parse(prev["ts"])
        if gap > timedelta(minutes=REWORK_MIN):
            continue
        text = ""
        try:
            snap = json.loads(nxt["snapshot"] or "{}")
            text = str(snap.get("prompt") or snap.get("user") or "")
        except (ValueError, AttributeError):
            text = ""
        low = text.lower()
        if any(m in low for m in REWORK_MARKERS):
            out.append({"rule": "rework_marker",
                        "evidence": f"ход {nxt['turn_no']} после {prev['turn_no']} через"
                                    f" {int(gap.total_seconds() // 60)} мин: {text[:80]}"})
    expired = c.execute(
        "SELECT id, kind, summary FROM approvals WHERE session_id=? AND status='pending'"
        " AND ts <= ?", (session_id, (datetime.now(timezone.utc) - timedelta(minutes=15))
                         .isoformat(timespec="seconds").replace("+00:00", "Z"))).fetchall()
    for ap in expired:
        out.append({"rule": "open_loop_approval",
                    "evidence": f"заявка #{ap['id']} {ap['kind']} протухла pending:"
                                f" {ap['summary']}"[:160]})
    return out


def chat_proxy_verdict(session_id: str, conn=None) -> tuple[str, list[dict]]:
    """Proxy-вердикт правил для чата: предохранитель на случай недоступности судьи.

    Действующая метка владельца перевешивает правила (приёмка — факт, а не вывод):
    «не то» = fail, «принято» = success. Метка старше последних ходов золото не
    несёт — остаётся сигналом в evidence, а вердикт считают правила.
    """
    c = conn or _c()
    rows = c.execute(
        "SELECT verdict FROM turn_verdict WHERE session_id=? AND rules_version=?",
        (session_id, RULES_VERSION)).fetchall()
    signals = chat_signals(session_id, c)
    eff = owner_effect(session_id, c)
    if eff.get("mark"):
        signals.append({"rule": "owner_mark", "evidence": mark_evidence(eff)})
    if eff.get("status"):
        return eff["status"], signals
    if not rows:
        return "unknown", signals
    verdicts = [r["verdict"] for r in rows]
    if "fail" in verdicts or any(s["rule"] == "open_loop_approval" for s in signals):
        return "fail", signals
    if "partial" in verdicts or any(s["rule"] == "rework_marker" for s in signals):
        return "partial", signals
    return "success", signals


def is_trial_meta(meta_raw) -> bool:
    """Пробная/нагрузочная сессия: признак trial в sessions.meta (BACKLOG-15 п.5).

    Синтетика в выборке делала долю fail неиспользуемой: 38% (18 из 47) считались
    вместе с нагрузочными «Выдай нумерованный список от 1 до 350» и «default-agent
    probe false», а среди 16 расхождений с правилами минимум два — такие же пробы.
    Стоячее правило контура: quality-метрика без фильтра синтетики — не прибор.
    """
    try:
        m = json.loads(meta_raw or "{}")
    except (ValueError, TypeError):
        return False
    return bool(isinstance(m, dict) and m.get("trial"))


def closed_chats(idle_hours: float = 6.0, limit: int = 40, conn=None) -> list[str]:
    """Чаты без ходов дольше idle_hours, suspect-first (сигналы и agent/run первыми).

    Пробные сессии исключаются ДО капа: иначе синтетика вытесняет живые чаты из
    выборки судьи, а не просто добавляется к ней.
    """
    c = conn or _c()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=idle_hours)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = c.execute(
        "SELECT s.id, s.meta, MAX(t.ts) AS last_ts FROM sessions s"
        " JOIN turns t ON t.session_id=s.id"
        " WHERE s.status != 'closed' GROUP BY s.id HAVING last_ts <= ? ORDER BY last_ts",
        (cutoff,)).fetchall()
    rows = [r for r in rows if not is_trial_meta(r["meta"])]
    scored = []
    for r in rows[:limit * 3]:
        sig = chat_signals(r["id"], c)
        heavy = c.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id=? AND turn_kind IN ('run','schedule')",
            (r["id"],)).fetchone()[0]
        scored.append((-(len(sig) * 2 + (1 if heavy else 0)), r["last_ts"], r["id"]))
    scored.sort()
    return [sid for _, _, sid in scored[:limit]]


def latency_summary(days: int = 7, conn=None) -> dict:
    """Сколько времени модель отвечала: p50/p95 всего и по классам/тираМ (мс)."""
    c = conn or _c()
    since = (datetime.now(timezone.utc) - timedelta(days=days)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = c.execute(
        "SELECT ms, task_class, tier_used FROM turns WHERE ts >= ?"
        " AND COALESCE(turn_kind,'') != 'judge' AND ms > 0", (since,)).fetchall()

    def pct(vals, q):
        if not vals:
            return 0
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(len(vals) * q))]

    all_ms = [r["ms"] for r in rows]
    by_class: dict[str, list[int]] = {}
    by_tier: dict[str, list[int]] = {}
    for r in rows:
        by_class.setdefault(r["task_class"] or "chat", []).append(r["ms"])
        by_tier.setdefault(r["tier_used"] or "?", []).append(r["ms"])
    return {"n": len(all_ms), "p50_ms": pct(all_ms, 0.5), "p95_ms": pct(all_ms, 0.95),
            "by_class": {k: {"n": len(v), "p50_ms": pct(v, 0.5), "p95_ms": pct(v, 0.95)}
                         for k, v in sorted(by_class.items(), key=lambda kv: -len(kv[1]))},
            "by_tier": {k: {"n": len(v), "p50_ms": pct(v, 0.5), "p95_ms": pct(v, 0.95)}
                        for k, v in sorted(by_tier.items(), key=lambda kv: -len(kv[1]))}}


def class_model_matrix(days: int = 7, conn=None) -> list[dict]:
    """Направление × модель: успех, токены, латентность — evidence для override."""
    c = conn or _c()
    since = (datetime.now(timezone.utc) - timedelta(days=days)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = c.execute(
        "SELECT task_class cls, model, tier_used tier, COUNT(*) n, SUM(ok) ok,"
        " AVG(ms) avg_ms, SUM(prompt_tokens + completion_tokens) tok"
        " FROM turns WHERE ts >= ? AND COALESCE(turn_kind,'') != 'judge'"
        " GROUP BY task_class, model, tier_used ORDER BY n DESC", (since,)).fetchall()
    return [{"class": r["cls"] or "chat", "model": r["model"], "tier": r["tier"],
             "n": r["n"], "ok": r["ok"] or 0,
             "success_rate": round(100.0 * (r["ok"] or 0) / r["n"], 1) if r["n"] else 0.0,
             "avg_ms": int(r["avg_ms"] or 0), "tokens": r["tok"] or 0} for r in rows]


def owner_mark(session_id: str, mark: str, note: str = "", conn=None) -> int:
    """Строка журнала меток владельца (ok | notit | none, где none = метка снята).

    Журнал, а не флаг: история меток остаётся видимой, а действующей считается
    последняя строка. До 02.11.2026 таблицу не писал никто, кроме selftest, —
    приёмка владельцем была не предусмотрена поверхностью (0 строк за всё время),
    и судья ставил partial за неподтверждённую приёмку.
    """
    if mark not in MARKS:
        raise ValueError(f"mark должен быть одним из {'|'.join(MARKS)}, дано {mark!r}")
    c = conn or _c()
    cur = c.execute("INSERT INTO owner_marks(ts, session_id, mark, note) VALUES(?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                     session_id, mark, note))
    c.commit()
    return cur.lastrowid


def owner_marks(session_id: str, conn=None, limit: int = 5) -> list[dict]:
    """Журнал меток чата, свежие первыми."""
    c = conn or _c()
    rows = c.execute(
        "SELECT id, ts, mark, note FROM owner_marks WHERE session_id=?"
        " ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "mark": r["mark"], "note": r["note"] or ""}
            for r in rows]


def owner_state(session_id: str, conn=None) -> dict | None:
    """Действующая метка = последняя строка журнала; `none` гасит прежнюю."""
    c = conn or _c()
    row = c.execute(
        "SELECT id, ts, mark, note FROM owner_marks WHERE session_id=?"
        " ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
    if row is None or row["mark"] not in GOLD_MARKS:
        return None
    return {"id": row["id"], "ts": row["ts"], "mark": row["mark"],
            "note": row["note"] or ""}


def turns_after(session_id: str, ts: str, conn=None) -> int:
    """Сколько ходов было ПОСЛЕ метки: метка до последних ходов уже не приёмка."""
    c = conn or _c()
    row = c.execute(
        "SELECT COUNT(*) AS n FROM session_turns WHERE session_id=? AND ts>?",
        (session_id, ts)).fetchone()
    return int(row["n"] or 0) if row else 0


def owner_effect(session_id: str, conn=None) -> dict:
    """Что метка владельца меняет в вердикте.

    `status` — детерминированный вердикт от метки ("" если метка не решает):
    принявший владелец закрывает вопрос приёмки, «не то» — вопрос результата.
    `stale` — после метки были ходы: тогда метка остаётся уликой о части чата,
    а вердикт выводят правила и судья (иначе вчерашнее «принято» навсегда
    оправдывало бы любой последующий развал).
    """
    st = owner_state(session_id, conn)
    if not st:
        return {"mark": "", "ts": "", "note": "", "stale": False, "status": ""}
    out = dict(st)
    out["stale"] = turns_after(session_id, st["ts"], conn) > 0
    out["status"] = "" if out["stale"] else MARK_STATUS[st["mark"]]
    return out


def mark_evidence(eff: dict) -> str:
    """Одна строка о метке — в сигналы правил и в транскрипт судье."""
    if not eff.get("mark"):
        return ""
    note = str(eff.get("note") or "").strip()
    tail = (" (метка поставлена до последних ходов — приёмкой всего чата"
            " не является)" if eff.get("stale") else "")
    return (f"владелец: {MARK_WORD.get(eff['mark'], eff['mark'])} {eff.get('ts') or ''}"
            + (f" «{note[:120]}»" if note else "") + tail)

