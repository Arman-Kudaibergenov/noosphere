"""Selftest аудита успешности чатов: фикстуры в своей sqlite, без сети и без LLM.

Запуск из корня noosphere:
    python scripts/audit_selftest.py
Проверяет: миграция на текущую SCHEMA_VERSION создаёт таблицы; правила
TECH/TOOL/COST ловят свои сигналы; переделка и протухшая заявка дают сигналы
чата; proxy-вердикт складывается; маска транскрипта режет Bearer/длинные
токены/пути секретов; разбор ответа судьи терпит markdown-обёртку и бракует мусор.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import chat_judge, db, transcript, verdicts  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

# Фикстуры привязаны к «сейчас», а не к дате написания. С литералами 12.10 три
# проверки (latency, matrix, report_harness) краснели вечно: их окна — последние
# 7 дней, а фикстуры уезжали из окна через неделю после написания. Красный
# прибор хуже отсутствия прибора — за ним перестают следить.
_BASE = datetime.now(timezone.utc) - timedelta(hours=3)


def _ts(minutes: int, seconds: int = 0) -> str:
    """Метка фикстуры: в окне последних 7 дней, но не сейчас — заявка обязана
    успеть протухнуть (15 мин), а переделка — попасть в окно 30 мин."""
    return (_BASE + timedelta(minutes=minutes, seconds=seconds)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")


def _day(minutes: int, seconds: int = 0) -> str:
    return (_BASE + timedelta(minutes=minutes, seconds=seconds)).date().isoformat()


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"CHECK {name} {detail} ok={ok}")
    return ok


def fixture() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    db.get_db = lambda: conn      # сухой прогон: router/optimizer читают ту же БД
    verdicts.set_conn(conn)
    return conn


def seed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO sessions(id, created_at, updated_at, status) VALUES"
                 " ('s1',?,?,?)", (_ts(0), _ts(30), "active"))
    conn.execute("INSERT INTO session_turns(session_id, turn_no, ts, status) VALUES"
                 " ('s1',1,?,?),('s1',2,?,?)",
                 (_ts(0), "done", _ts(10), "done"))
    # ход 1: чистый; ход 2: обрыв по length + rc=1 инструмента; ход 3: переделка с маркером
    conn.execute("INSERT INTO turns(ts, day, session_id, task_class, tier_start, tier_used,"
                 " model, channel, ok, answered, finish, prompt_tokens, completion_tokens,"
                 " ms, err, escalated, content_filter) VALUES"
                 " (?,?,'s1','code','mid','mid','m','alibaba',1,1,'stop',100,200,5000,'',0,0),"
                 " (?,?,'s1','code','mid','deep','m','alibaba',1,1,'length',100,4000,9000,'',1,0)",
                 (_ts(0, 10), _day(0, 10), _ts(10, 10), _day(10, 10)))
    conn.execute("INSERT INTO tool_audit(ts, session_id, tool, args_digest, out_len,"
                 " turn_no, rc, args_summary) VALUES"
                 " (?,'s1','shell_exec','d',10,2,1,'rm -rf /tmp/x')", (_ts(10, 20),))
    conn.execute("INSERT INTO approvals(ts, session_id, kind, summary, cmd_hash, status) VALUES"
                 " (?,'s1','git-push','git push origin main','h','pending')", (_ts(-50),))
    conn.execute("INSERT INTO session_turns(session_id, turn_no, ts, status, snapshot)"
                 " VALUES ('s1',3,?,?,?)",
                 (_ts(15), "done",
                  json.dumps({"prompt": "не то, переделай заново"}, ensure_ascii=False)))
    conn.commit()


def main() -> int:
    conn = fixture()
    ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    check("migration", ver is not None and int(ver["value"]) == db.SCHEMA_VERSION,
          f"schema_version={ver['value'] if ver else None} expected={db.SCHEMA_VERSION}")

    seed(conn)
    t1 = verdicts.eval_turn("s1", 1, conn)
    t2 = verdicts.eval_turn("s1", 2, conn)
    check("rules_clean_turn", t1["verdict"] == "success" and not t1["signals"],
          f"verdict={t1['verdict']}")
    rules2 = {s["rule"] for s in t2["signals"]}
    check("rules_fail_turn", t2["verdict"] == "fail"
          and {"tech_length", "tool_rc"} <= rules2, f"signals={sorted(rules2)}")

    verdicts.recompute_turn("s1", 1, conn)
    verdicts.recompute_turn("s1", 2, conn)
    rows = conn.execute("SELECT COUNT(*) n FROM turn_verdict WHERE session_id='s1'").fetchone()
    sig = conn.execute("SELECT COUNT(*) n FROM verdict_signals WHERE session_id='s1'").fetchone()
    check("verdict_rows", rows["n"] == 2 and sig["n"] >= 2, f"turns={rows['n']} signals={sig['n']}")

    signals = verdicts.chat_signals("s1", conn)
    kinds = {s["rule"] for s in signals}
    check("chat_signals", {"rework_marker", "open_loop_approval"} <= kinds,
          f"signals={sorted(kinds)}")
    proxy, _ = verdicts.chat_proxy_verdict("s1", conn)
    check("proxy_verdict", proxy == "fail", f"proxy={proxy}")

    text, stats = transcript.condensed("s1", conn)
    check("transcript_stats", stats["turns"] >= 1 and stats["tools"] == 1,
          f"stats={stats['turns']}/{stats['tools']}")
    dirty = ("секрет Bearer sk-abcdef1234567890abcdef1234567890abcdef12 и"
             " путь ~/.secrets/noosphere.token внутри")
    clean = transcript.mask(dirty)
    check("mask", "sk-abcdef" not in clean and ".secrets/noosphere.token" not in clean
          and "Bearer sk-" not in clean, f"masked={clean[:60]}")

    good = chat_judge.parse_verdict('```json\n{"status":"partial","reason":"половина",'
                                    '"soft_status":"success","soft_reason":"польза есть",'
                                    '"evidence_turns":[2],"confidence":0.7}\n```')
    bad = chat_judge.parse_verdict("статус: примерно успех")
    check("judge_parse", bool(good) and good["status"] == "partial"
          and good["soft_status"] == "success"
          and good["evidence_turns"] == [2] and bad is None,
          f"good={good is not None} soft={good and good['soft_status']} bad={bad is None}")

    marks = verdicts.owner_mark("s1", "notit", "решил не то", conn)
    check("owner_mark", marks >= 1, f"id={marks}")

    # ── приёмка владельцем: метка перевешивает правила (своя сессия, чтобы не
    # менять фикстуры s1, на которых считаются остальные проверки) ──
    conn.execute("INSERT INTO sessions(id, created_at, updated_at, status) VALUES"
                 " ('s2',?,?,?)", (_ts(0), _ts(6), "active"))
    conn.execute("INSERT INTO session_turns(session_id, turn_no, ts, status) VALUES"
                 " ('s2',1,?,'done')", (_ts(5),))
    conn.commit()
    verdicts.recompute_turn("s2", 1, conn)
    p_before, _ = verdicts.chat_proxy_verdict("s2", conn)
    verdicts.owner_mark("s2", "notit", "сделало не то", conn)
    p_marked, sig_marked = verdicts.chat_proxy_verdict("s2", conn)
    check("owner_mark_overrides_rules", p_before == "success" and p_marked == "fail"
          and any(s["rule"] == "owner_mark" for s in sig_marked),
          f"before={p_before} marked={p_marked}")
    eff = verdicts.owner_effect("s2", conn)
    txt2, stats2 = transcript.condensed("s2", conn)
    check("owner_mark_in_transcript", eff["status"] == "fail" and eff["stale"] is False
          and "[метки владельца]" in txt2 and stats2["owner_mark"] == "notit"
          and "метка владельца" in chat_judge.build_prompt(txt2, stats2),
          f"eff={eff['mark']}/{eff['status']} block={'[метки владельца]' in txt2}")
    clamped = chat_judge._clamp_by_mark(
        {"status": "success", "reason": "всё получилось", "soft_status": "success",
         "soft_reason": "", "evidence_turns": [1], "confidence": 0.9}, stats2)
    check("owner_mark_clamps_judge", clamped["status"] == "fail"
          and clamped["soft_status"] == "fail" and "всё получилось" in clamped["reason"],
          f"status={clamped['status']}")
    soft_only = chat_judge._clamp_by_mark(
        {"status": "fail", "reason": "судья сам сказал fail", "soft_status": "partial",
         "soft_reason": "прогресс был", "evidence_turns": [1], "confidence": 0.6}, stats2)
    check("owner_mark_aligns_soft", soft_only["status"] == "fail"
          and soft_only["soft_status"] == "fail"
          and soft_only["reason"] == "судья сам сказал fail",
          f"soft={soft_only['soft_status']} hard={soft_only['status']}")

    # ── оптимизатор: белый список, риск, применение ──
    from server import optimizer, router

    good = optimizer.parse_contracts(json.dumps([
        {"target": "class_tier_override", "diff": {"class": "chat", "tier": "mid"},
         "evidence": "success=4/5, p90 токенов base=91%", "risk": "A1"},
        {"target": "project_keywords", "diff": {"project": "Личное", "keyword": "квоты"},
         "evidence": "override 3 раза за 7 дней", "risk": "A1"},
        {"target": "shell_exec_policy", "diff": {}, "evidence": "x", "risk": "A3"},
        {"target": "judge_prompt", "diff": {"text": "коротко"}, "evidence": "x", "risk": "A2"},
        {"target": "class_tier_override", "diff": {"class": "nosuchclass", "tier": "mid"},
         "evidence": "junk", "risk": "A2"},
    ]))
    check("optimizer_whitelist",
          [g["target"] for g in good] == ["class_tier_override", "project_keywords"]
          and good[0]["risk"] == "A2",
          f"kept={[g['target'] for g in good]} risk0={good[0]['risk']}")

    conn.execute("INSERT INTO projects(name, keywords) VALUES('Личное','')")
    conn.commit()
    ids = optimizer.store(good, conn)
    decided = optimizer.decide(ids[0], True, conn=conn)
    applied2 = optimizer.decide(ids[1], True, conn=conn)
    check("optimizer_apply",
          decided.get("ok") and applied2.get("ok")
          and router.start_tier("chat") == "mid"
          and "квоты" in conn.execute(
              "SELECT keywords FROM projects WHERE name='Личное'").fetchone()["keywords"],
          f"tier(chat)={router.start_tier('chat')}")
    router.reset_override_cache()
    denied = optimizer.decide(ids[0], True, conn=conn)
    check("optimizer_once", not denied.get("ok"), f"second={denied.get('error')}")
    dup = optimizer._already_in_effect(
        {"target": "class_tier_override", "diff": {"class": "chat", "tier": "mid"}})
    check("optimizer_dedupe", dup is True, f"dup={dup}")

    # ── вентиль A1: три ночи одинаковых evidence → авто-применение ──
    optimizer.toggle_a1("project_keywords", conn)
    diff = json.dumps({"project": "Личное", "keyword": "квоты"}, ensure_ascii=False)
    for day in ("2026-10-10", "2026-10-11", "2026-10-12"):
        conn.execute("INSERT INTO audit_contracts(ts, target, risk, diff_json,"
                     " evidence_json, status) VALUES(?,?, 'A1', ?, '{}', 'pending')",
                     (f"{day}T03:00:00Z", "project_keywords", diff))
    conn.commit()
    applied = optimizer.auto_apply_a1(conn)
    kw = conn.execute("SELECT keywords FROM projects WHERE name='Личное'").fetchone()["keywords"]
    check("a1_auto_apply", len(applied) == 1 and "квоты" in kw,
          f"applied={applied} keywords={kw}")
    optimizer.toggle_a1("project_keywords", conn)
    check("a1_toggle_off", optimizer.a1_state(conn)["project_keywords"] is False)

    # ── недельный отчёт рендерится из фикстур ──
    from server import audit_report, harness_usage

    page = audit_report.build_html(7)
    check("report_html", "Аудит успешности чатов" in page and "success-rate" in page,
          f"bytes={len(page)}")

    # ── ингест харнесов: дедуп через ingest_seen ──
    row = {"sessionId": "h1", "timestamp": int(_BASE.timestamp() * 1000),
           "models": {"deepseek-v4.1-flash": {"requests": 3, "inputTokens": 100,
                                              "outputTokens": 50, "cachedTokens": 20}},
           "tools": {"byName": {"read_file": {"count": 2}}},
           "skills": {"byName": {"review": {"count": 1}}},
           "files": {"linesAdded": 7, "linesRemoved": 2}}
    first = harness_usage.ingest_rows([row])
    second = harness_usage.ingest_rows([row])
    check("harness_dedupe", first == {"added": 1, "skipped": 0}
          and second == {"added": 0, "skipped": 1}, f"{first} {second}")

    page2 = audit_report.build_html(7)
    check("report_harness", "Второй контур" in page2
          and page2.count("<tr><td>harness</td>") >= 1,
          f"bytes={len(page2)} rows={page2.count('<tr><td>harness</td>')}")

    # ── deep-dive: кап 5 чатов, max-тир, режим deep ──
    import asyncio as _aio

    tiers: list[str] = []

    async def fake_deep(task_class, prompt, **kw):
        tiers.append(kw.get("tier", ""))
        return {"ok": True, "model": "qwen3.8-max", "tier": kw.get("tier", "max"),
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                "text": '{"status":"fail","reason":"deep-разбор","soft_status":"partial",'
                        '"soft_reason":"прогресс был","evidence_turns":[1,2],'
                        '"confidence":0.9}'}

    chat_judge.pool.ask = fake_deep  # noqa: SLF001
    outs = _aio.run(chat_judge.deep_dive(["s1"] * 8))
    mode = conn.execute("SELECT mode, reason FROM chat_verdict WHERE session_id='s1'") \
        .fetchone()
    check("deep_dive_cap", len(outs) == 5 and set(tiers) == {"max"}
          and mode["mode"] == "deep" and mode["reason"].startswith("deep-dive:"),
          f"outs={len(outs)} tiers={sorted(set(tiers))} mode={mode['mode']}")

    # ── пакет D: латентность, матрица направления×модели, доступы по компаниям ──
    lat = verdicts.latency_summary(7, conn)
    check("latency", lat["n"] == 2 and lat["p50_ms"] in (5000, 9000)
          and lat["by_class"].get("code", {}).get("n") == 2,
          f"n={lat['n']} p50={lat['p50_ms']} p95={lat['p95_ms']}")
    mx = verdicts.class_model_matrix(7, conn)
    check("matrix", any(r["class"] == "code" and r["model"] == "m" for r in mx),
          f"rows={len(mx)}")

    mcontract = optimizer.parse_contracts(json.dumps([
        {"target": "class_model_override",
         "diff": {"class": "code", "model": "deepseek-v4-pro"},
         "evidence": "code на m: success 0/2", "risk": "A2"}]))
    mid = optimizer.store(mcontract, conn)
    mres = optimizer.decide(mid[0], True, conn=conn)
    check("model_override", mres.get("ok")
          and router.model_override_for("code") == "deepseek-v4-pro",
          f"override={router.model_override_for('code')}")
    router.reset_override_cache()

    from server import vault

    vid = vault.upsert("login", "probe-biocard", host="h1", login="u1", company="BioCard")
    owner_rows = vault.list_for_owner()
    check("vault_company", any(r["id"] == vid and r["company"] == "BioCard"
                               and r["notes"] is not None for r in owner_rows),
          f"rows={len(owner_rows)}")
    vault.set_company(vid, "ТестКонтур")
    check("vault_set_company",
          vault.list_for_owner()[0]["company"] == "ТестКонтур"
          if len(vault.list_for_owner()) == 1 else
          any(r["company"] == "ТестКонтур" for r in vault.list_for_owner()))

    verdicts.set_conn(None)
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print(f"AUDIT SELFTEST FAIL {failed}")
        return 1
    print(f"AUDIT SELFTEST PASS ({len(RESULTS)}/{len(RESULTS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
