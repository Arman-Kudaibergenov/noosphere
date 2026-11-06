#!/usr/bin/env python3
"""judge_review.py — еженедельный разбор прибора успешности чатов (BACKLOG-15 п.5в).

Печатает три вещи, без которых доля fail не интерпретируется:
1. распределение вердиктов БЕЗ пробных сессий (синтетика исключается по
   sessions.meta.trial) рядом с распределением «со синтетикой»;
2. покрытие confidence: сколько вердиктов с числом, сколько NULL (судья не вернул)
   и сколько ровных нулей;
3. расхождения судьи с правилами (rules_agree=0) с причиной судьи и сигналами
   правил по ходам — по ним видно, кто врёт: правила или судья.

Read-only: открывает БД в режиме mode=ro, ничего не пишет.
Запуск из корня репо: ./.venv/bin/python scripts/judge_review.py [--days 30] [--top 5]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import load_config  # noqa: E402


def _conn() -> sqlite3.Connection:
    path = load_config().data_dir / "noosphere.db"
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _is_trial(meta_raw) -> bool:
    try:
        m = json.loads(meta_raw or "{}")
    except (ValueError, TypeError):
        return False
    return bool(isinstance(m, dict) and m.get("trial"))


def _dist(rs) -> str:
    d: dict[str, int] = {}
    for r in rs:
        k = r["status"] or "-"
        d[k] = d.get(k, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1])) or "—"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=5, help="сколько расхождений разобрать")
    a = ap.parse_args()
    c = _conn()

    rows = c.execute(
        "SELECT v.*, COALESCE(s.meta,'') AS session_meta, COALESCE(s.title,'') AS title"
        " FROM chat_verdict v LEFT JOIN sessions s ON s.id=v.session_id"
        " ORDER BY v.ts DESC").fetchall()
    live = [r for r in rows if not _is_trial(r["session_meta"])]
    trials = [r for r in rows if _is_trial(r["session_meta"])]

    print("=== прибор успешности чатов: разбор ===")
    print(f"вердиктов всего: {len(rows)} | живых: {len(live)} | пробных: {len(trials)}")
    print(f"  живые          : {_dist(live)}")
    print(f"  со синтетикой  : {_dist(rows)}")
    judged = [r for r in live if r["status"] != "unresolved"]
    if judged:
        ok = sum(1 for r in judged if r["status"] == "success")
        fail = sum(1 for r in judged if r["status"] == "fail")
        print(f"  success среди решённых (без unresolved): {round(100.0*ok/len(judged),1)}% ({ok}/{len(judged)})")
        print(f"  fail среди решённых: {round(100.0*fail/len(judged),1)}% ({fail}/{len(judged)})")

    print()
    print("--- покрытие confidence ---")
    nulls = sum(1 for r in rows if r["confidence"] is None)
    zeros = sum(1 for r in rows if r["confidence"] == 0)
    real = [r["confidence"] for r in rows if r["confidence"] not in (None, 0)]
    print(f"  NULL (судья не вернул): {nulls} | ровно 0: {zeros} | содержательных: {len(real)}")
    if real:
        print(f"  средняя содержательная уверенность: {round(sum(real)/len(real),2)}")
    by_model: dict[str, list] = {}
    for r in rows:
        by_model.setdefault(r["judge_model"] or "-", []).append(r["confidence"])
    for m, vals in sorted(by_model.items(), key=lambda kv: -len(kv[1])):
        filled = sum(1 for v in vals if v not in (None, 0))
        print(f"  {m}: вердиктов {len(vals)}, с confidence {filled}")

    print()
    print(f"--- расхождения судьи с правилами (rules_agree=0), топ {a.top} ---")
    dis = [r for r in live if r["rules_agree"] == 0]
    print(f"  всего расхождений среди живых: {len(dis)} из {len(live)}")
    for r in dis[:a.top]:
        print(f"\n  ## {r['session_id'][:8]} «{(r['title'] or '')[:60]}» {str(r['ts'])[:16]}")
        print(f"     судья  : {r['status']} (soft={r['soft_status'] or '-'},"
              f" conf={r['confidence']}, модель={r['judge_model']}, mode={r['mode']})")
        print(f"     правила: {r['rules_proxy']}")
        print(f"     причина: {(r['reason'] or '')[:220]}")
        sig = c.execute(
            "SELECT turn_no, rule, substr(evidence,1,90) ev FROM verdict_signals"
            " WHERE session_id=? ORDER BY turn_no LIMIT 10", (r["session_id"],)).fetchall()
        for sg in sig:
            print(f"       сигнал: ход {sg['turn_no']} {sg['rule']}: {sg['ev']}")
        if not sig:
            print("       сигналов правил нет (proxy выставлен без evidence)")
    print("\nкак читать: причина судьи противоречит его же статусу или сигналы правил"
          "\nпоказывают tech_no_attempts/tool_rc — скорее правы правила; сигналы только"
          "\ncost_p90 при выполненной задаче — судья строже правил.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
