"""Ночной оптимизатор: агрегаты аудита → контракты изменений (дизайн 2026-10-12, §4).

Контракт = готовое изменение с evidence и классом риска; владелец утверждает одной
строкой (A2) или цель взводится тумблером после трёх ночей одинаковых evidence (A1).
Авто-применения без вентиля нет; A3 (бюджеты, промпты агента, shell, деплой, секреты)
в белый список целей не входит вовсе.

Цели и куда применяется:
  project_keywords   A1  projects.keywords += слово из override владельца
  class_tier_override A2  router.start_tier читает таблицу поверх CLASS_TIER
  judge_prompt       A2  новая версия промпта судьи (chat_judge берёт последнюю)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import db, router, verdicts

RISK = {"project_keywords": "A1", "class_tier_override": "A2",
        "class_model_override": "A2", "judge_prompt": "A2"}
CONTRACT_TTL_DAYS = 7
OPTIMIZER_PROMPT_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _c():
    return db.get_db()


def aggregate(days: int = 7, conn=None) -> dict:
    """Агрегаты для оптимизатора и дайджеста: 0 LLM-токенов."""
    c = conn or _c()
    since = (datetime.now(timezone.utc) - timedelta(days=days)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    out: dict = {"days": days, "classes": {}, "fail_rules": {}, "rework": 0}
    rows = c.execute(
        "SELECT t.task_class AS cls, COUNT(*) n,"
        " SUM(CASE WHEN v.verdict='success' THEN 1 ELSE 0 END) ok,"
        " SUM(CASE WHEN v.verdict='fail' THEN 1 ELSE 0 END) bad,"
        " SUM(t.prompt_tokens + t.completion_tokens) tokens"
        " FROM turns t LEFT JOIN turn_verdict v"
        "  ON v.session_id = t.session_id AND v.rules_version = ?"
        " WHERE t.ts >= ? AND COALESCE(t.turn_kind,'') != 'judge'"
        " GROUP BY t.task_class", (verdicts.RULES_VERSION, since)).fetchall()
    for r in rows:
        out["classes"][r["cls"] or "chat"] = {"turns": r["n"], "success": r["ok"] or 0,
                                              "fail": r["bad"] or 0, "tokens": r["tokens"] or 0}
    sig = c.execute(
        "SELECT rule, COUNT(*) n FROM verdict_signals WHERE ts >= ?"
        " GROUP BY rule ORDER BY n DESC LIMIT 8", (since,)).fetchall()
    out["fail_rules"] = {r["rule"]: r["n"] for r in sig}
    out["rework"] = c.execute(
        "SELECT COUNT(*) FROM verdict_signals WHERE rule='rework_marker' AND ts >= ?",
        (since,)).fetchone()[0]
    return out


def build_prompt(agg: dict) -> str:
    return ("Агрегаты аудита за " + str(agg["days"]) + " дней: "
            + json.dumps(agg, ensure_ascii=False)[:6000]
            + "\nПредложи НЕ БОЛЕЕ трёх контрактов оптимизации строго в JSON-массиве:"
              ' [{"target": "project_keywords|class_tier_override|class_model_override|judge_prompt",'
              ' "diff": {...}, "evidence": "числа из агрегатов", "risk": "A1|A2"}].'
              " target вне этого списка запрещён; diff для project_keywords ="
              ' {"project": имя, "keyword": слово}; для class_tier_override ='
              ' {"class": имя, "tier": base|mid|deep|max}; для class_model_override ='
              ' {"class": имя, "model": id модели из агрегатов}; для judge_prompt ='
              ' {"text": полный новый промпт}. Если улучшений нет — верни [].')


def parse_contracts(text: str) -> list[dict]:
    """Разбор ответа оптимизатора с жёсткой валидацией белого списка и риска."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return []
    out: list[dict] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "")
        risk = str(item.get("risk") or "").upper()
        diff = item.get("diff")
        evidence = str(item.get("evidence") or "").strip()
        if target not in RISK or not isinstance(diff, dict) or not evidence:
            continue
        if risk != RISK[target]:          # риск цели фиксирован, модель его не выбирает
            risk = RISK[target]
        if target == "class_tier_override" and (
                diff.get("tier") not in router.TIERS
                or diff.get("class") not in router.CLASS_TIER):
            continue  # junk-контракт на несуществующий класс не доезжает до утверждения
        if target == "class_model_override" and (
                diff.get("model") not in router.ALL_MODELS
                or diff.get("class") not in router.CLASS_TIER):
            continue
        if target == "judge_prompt" and len(str(diff.get("text") or "")) < 80:
            continue
        out.append({"target": target, "risk": risk, "diff": diff, "evidence": evidence[:400]})
    return out[:3]


def _already_in_effect(contract: dict) -> bool:
    """Контракт дублирует текущее состояние (override тира/модели уже такой)."""
    diff = contract.get("diff") or {}
    if contract.get("target") == "class_tier_override":
        return router.start_tier(str(diff.get("class") or "")) == diff.get("tier")
    if contract.get("target") == "class_model_override":
        return router.model_override_for(str(diff.get("class") or "")) == diff.get("model")
    return False


def store(contracts: list[dict], conn=None) -> list[int]:
    c = conn or _c()
    ids = []
    for item in contracts:
        cur = c.execute(
            "INSERT INTO audit_contracts(ts, target, risk, diff_json, evidence_json,"
            " status) VALUES(?,?,?,?,?,'pending')",
            (_now(), item["target"], item["risk"], json.dumps(item["diff"], ensure_ascii=False),
             json.dumps({"evidence": item["evidence"],
                         "optimizer_version": OPTIMIZER_PROMPT_VERSION}, ensure_ascii=False)))
        ids.append(cur.lastrowid)
    c.commit()
    return ids


def expire_stale(conn=None) -> int:
    c = conn or _c()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CONTRACT_TTL_DAYS)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    cur = c.execute("UPDATE audit_contracts SET status='expired' WHERE status='pending'"
                    " AND ts <= ?", (cutoff,))
    c.commit()
    return cur.rowcount


def apply_contract(contract_id: int, conn=None) -> dict:
    """Применяет утверждённый контракт; идемпотентно (повтор = та же строка)."""
    c = conn or _c()
    row = c.execute("SELECT * FROM audit_contracts WHERE id=?", (contract_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "контракт не найден"}
    if row["status"] not in ("approved", "applied"):
        return {"ok": False, "error": f"статус {row['status']} не применяется"}
    diff = json.loads(row["diff_json"] or "{}")
    if row["target"] == "project_keywords":
        name, word = str(diff.get("project") or ""), str(diff.get("keyword") or "").strip()
        if not name or not word:
            return {"ok": False, "error": "нужны project и keyword"}
        proj = c.execute("SELECT id, keywords FROM projects WHERE name=?", (name,)).fetchone()
        if proj is None:
            return {"ok": False, "error": f"проекта {name} нет"}
        words = [w for w in (proj["keywords"] or "").split(",") if w.strip()]
        if word not in words:
            words.append(word)
            c.execute("UPDATE projects SET keywords=? WHERE id=?",
                      (",".join(words), proj["id"]))
    elif row["target"] == "class_tier_override":
        cls, tier = str(diff.get("class") or ""), str(diff.get("tier") or "")
        if cls not in router.CLASS_TIER or tier not in router.TIERS:
            return {"ok": False, "error": f"неизвестная пара {cls}/{tier}"}
        c.execute("INSERT OR REPLACE INTO class_tier_override(class, tier, reason, ts)"
                  " VALUES(?,?,?,?)", (cls, tier, row["evidence_json"][:200], _now()))
        router.reset_override_cache()
    elif row["target"] == "class_model_override":
        cls, model = str(diff.get("class") or ""), str(diff.get("model") or "")
        if cls not in router.CLASS_TIER or model not in router.ALL_MODELS:
            return {"ok": False, "error": f"неизвестная пара {cls}/{model}"}
        c.execute("INSERT OR REPLACE INTO class_model_override(class, model, reason, ts)"
                  " VALUES(?,?,?,?)", (cls, model, row["evidence_json"][:200], _now()))
        router.reset_override_cache()
    elif row["target"] == "judge_prompt":
        text = str(diff.get("text") or "")
        if len(text) < 80:
            return {"ok": False, "error": "промпт короче 80 символов"}
        from .chat_judge import JUDGE_PROMPT_VERSION
        top = c.execute("SELECT MAX(version) v FROM judge_prompts").fetchone()["v"] or 0
        # BACKLOG-15 п.20: утверждённый контракт пишется ВЫШЕ пола кодовой версии —
        # иначе встроенный промпт перекроет текст, который владелец только что утвердил.
        c.execute("INSERT OR REPLACE INTO judge_prompts(version, text, ts) VALUES(?,?,?)",
                  (max(top, JUDGE_PROMPT_VERSION) + 1, text, _now()))
    else:
        return {"ok": False, "error": f"цель {row['target']} вне белого списка"}
    c.execute("UPDATE audit_contracts SET status='applied', applied_at=? WHERE id=?",
              (_now(), contract_id))
    c.commit()
    return {"ok": True, "target": row["target"]}


def decide(contract_id: int, approve: bool, by: str = "owner", conn=None) -> dict:
    c = conn or _c()
    row = c.execute("SELECT status FROM audit_contracts WHERE id=?", (contract_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "контракт не найден"}
    if row["status"] != "pending":
        return {"ok": False, "error": f"статус {row['status']}"}
    if not approve:
        c.execute("UPDATE audit_contracts SET status='denied', decided_by=?, decided_at=?"
                  " WHERE id=?", (by, _now(), contract_id))
        c.commit()
        return {"ok": True, "status": "denied"}
    c.execute("UPDATE audit_contracts SET status='approved', decided_by=?, decided_at=?"
              " WHERE id=?", (by, _now(), contract_id))
    c.commit()
    return apply_contract(contract_id, c)


def pending(conn=None) -> list[dict]:
    c = conn or _c()
    return [dict(r) for r in c.execute(
        "SELECT id, ts, target, risk, diff_json, evidence_json, status FROM audit_contracts"
        " WHERE status='pending' ORDER BY id")]


async def run_phase(agg: dict | None = None) -> tuple[list[int], str]:
    """Фаза 3 ночи: агрегаты → контракты. Возвращает (ids, строка для дайджеста)."""
    from . import chat_judge, pool

    agg = agg or aggregate()
    res = await pool.ask("audit", build_prompt(agg), system=chat_judge.JUDGE_SYSTEM,
                         force_no_escalate=True, check_json=True, max_tokens=900,
                         turn_kind="judge", role="judge")
    contracts = parse_contracts(res.get("text") or "") if res else []
    # не предлагать то, что уже действует: окно агрегатов ещё помнит провалы до override
    contracts = [c for c in contracts if not _already_in_effect(c)]
    ids = store(contracts) if contracts else []
    applied = auto_apply_a1()
    expire_stale()
    tail = f", авто-применено A1: {applied}" if applied else ""
    if not ids:
        return ids, f"контрактов оптимизации за ночь нет{tail}"
    lines = ", ".join(f"#{i} {c['target']}({c['risk']})"
                      for i, c in zip(ids, contracts))
    return ids, (f"контракты на утверждение: {lines} — approve:"
                 f" POST /v1/audit/contracts/<id>/approve{tail}")


# ── вентили A1: авто-применение обратимых целей после трёх ночей одинаковых evidence ──
# Сюда сознательно входит только то, что не исполняет код и откатывается одной строкой;
# гашение скилов/MCP добавится вместе с реестрами контрольной плоскости (батч B1).
A1_TARGETS = ("project_keywords",)


def a1_key(target: str) -> str:
    return "audit_a1_" + target


def a1_state(conn=None) -> dict:
    c = conn or _c()
    out = {}
    for t in A1_TARGETS:
        row = c.execute("SELECT value FROM meta WHERE key=?", (a1_key(t),)).fetchone()
        out[t] = bool(row and row["value"] == "1")
    return out


def toggle_a1(target: str, conn=None) -> dict:
    if target not in A1_TARGETS:
        return {"ok": False, "error": f"цель {target} не входит в A1"}
    c = conn or _c()
    cur = a1_state(c).get(target, False)
    c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
              (a1_key(target), "0" if cur else "1"))
    c.commit()
    return {"ok": True, "a1": a1_state(c)}


def auto_apply_a1(conn=None) -> list[int]:
    c = conn or _c()
    applied: list[int] = []
    for target, on in a1_state(c).items():
        if not on:
            continue
        done_diffs: set[str] = set()
        pend = c.execute(
            "SELECT id, diff_json FROM audit_contracts WHERE target=? AND status='pending'"
            " ORDER BY id DESC", (target,)).fetchall()
        for p in pend:
            if p["diff_json"] in done_diffs:
                continue  # одни и те же evidence применяются одним контрактом (свежайшим)
            nights = c.execute(
                "SELECT COUNT(DISTINCT date(ts)) n FROM audit_contracts"
                " WHERE target=? AND diff_json=?", (target, p["diff_json"])).fetchone()["n"]
            if nights >= 3:
                if decide(p["id"], True, by="auto-a1", conn=c).get("ok"):
                    applied.append(p["id"])
                    done_diffs.add(p["diff_json"])
    return applied
