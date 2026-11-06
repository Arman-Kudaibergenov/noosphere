"""Dry-run ночного судьи: фикстуры в своей sqlite, pool.ask подменён заглушкой.

Проверяет боевой путь C2 без сети и без траты токенов: очередь закрытых чатов
(suspect-first), запись chat_verdict, пометку proxy при невалидном ответе судьи,
гейт «раз в сутки» и текст дайджеста.
Запуск из корня noosphere: python scripts/audit_judge_dryrun.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import chat_judge, db, verdicts  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"CHECK {name} {detail} ok={ok}")
    return ok


def old_ts(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> int:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    db.get_db = lambda: conn          # сухой прогон: боевая БД не трогается
    verdicts.set_conn(conn)

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn.execute("INSERT INTO sessions(id, created_at, updated_at, status) VALUES"
                 " ('closed1',?,?,'active'), ('closed2',?,?,'active')",
                 (old_ts(30), old_ts(10), old_ts(30), old_ts(9)))
    for sid, hours in (("closed1", 10), ("closed2", 9)):
        conn.execute("INSERT INTO session_turns(session_id, turn_no, ts, status, snapshot)"
                     " VALUES(?,1,?,'done',?)",
                     (sid, old_ts(hours), '{"prompt": "собери отчёт по квотам",'
                                          ' "text": "готово, вот цифры"}'))
        conn.execute("INSERT INTO turns(ts, day, session_id, task_class, tier_start,"
                     " tier_used, model, channel, ok, answered, finish, prompt_tokens,"
                     " completion_tokens, ms, err, escalated, content_filter, turn_kind)"
                     " VALUES(?,?,?,'chat','base','base','m','alibaba',1,1,'stop',50,80,"
                     " 4000,'',0,0,'inline')", (old_ts(hours), day, sid))
    # closed2 — с сигналом переделки: suspect-first обязан поставить его первым
    conn.execute("INSERT INTO session_turns(session_id, turn_no, ts, status, snapshot)"
                 " VALUES('closed2',2,?,'done',?)",
                 (old_ts(8.9), '{"prompt": "не то, переделай"}'))
    conn.execute("INSERT INTO turns(ts, day, session_id, task_class, tier_start,"
                 " tier_used, model, channel, ok, answered, finish, prompt_tokens,"
                 " completion_tokens, ms, err, escalated, content_filter, turn_kind)"
                 " VALUES(?,?, 'closed2','chat','base','base','m','alibaba',1,1,'stop',"
                 " 50,80,4000,'',0,0,'inline')", (old_ts(8.9), day))
    conn.commit()

    order = verdicts.closed_chats(idle_hours=6.0, limit=10, conn=conn)
    check("queue_suspect_first", order[:1] == ["closed2"], f"order={order}")

    calls: list[str] = []

    async def fake_ask(task_class, prompt, **kw):
        calls.append(task_class)
        if kw.get("turn_kind") != "judge":
            return {"ok": False, "text": "", "usage": {}, "tier": "", "model": ""}
        return {"ok": True, "model": "deepseek-v4-pro", "tier": "deep",
                "usage": {"prompt_tokens": 2000, "completion_tokens": 120},
                "text": '{"status": "success", "reason": "задача решена",'
                        ' "evidence_turns": [1], "confidence": 0.8}'}

    chat_judge.pool.ask = fake_ask  # noqa: SLF001 — сухой прогон
    summary = asyncio.run(chat_judge.nightly(cap=5, idle_hours=6.0))
    rows = {r["session_id"]: dict(r) for r in conn.execute(
        "SELECT session_id, status, judge_model, tokens, rules_agree FROM chat_verdict")}
    check("judge_writes", rows.get("closed1", {}).get("status") == "success"
          and rows["closed1"]["judge_model"] == "deepseek-v4-pro"
          and rows["closed1"]["tokens"] == 2120,
          f"statuses={ {k: v['status'] for k, v in rows.items()} }")
    check("judge_class", calls and set(calls) == {"audit"}, f"classes={sorted(set(calls))}")
    check("digest_text", bool(summary) and "оценено 2" in summary, f"summary={summary}")

    # гейт «раз в сутки»: второй прогон той же ночью ничего не делает
    # (в calls: 2 суда + 1 прогон оптимизатора первой ночи)
    second = asyncio.run(chat_judge.nightly(cap=5, idle_hours=6.0))
    check("once_per_day", second is None and len(calls) == 3,
          f"second={second} calls={len(calls)}")

    # невалидный ответ судьи → proxy-вердикт правил с пометкой, а не пустота
    conn.execute("DELETE FROM meta WHERE key='audit_judge_day'")
    conn.execute("DELETE FROM chat_verdict")
    conn.commit()

    async def bad_ask(task_class, prompt, **kw):
        return {"ok": True, "model": "deepseek-v4-pro", "tier": "deep",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "text": "простите, не понял"}

    chat_judge.pool.ask = bad_ask  # noqa: SLF001
    asyncio.run(chat_judge.nightly(cap=5, idle_hours=6.0))
    proxy_rows = {r["session_id"]: dict(r) for r in conn.execute(
        "SELECT session_id, status, reason FROM chat_verdict")}
    check("proxy_on_bad_judge",
          all("proxy" in r["reason"] for r in proxy_rows.values()) and proxy_rows,
          f"statuses={ {k: v['status'] for k, v in proxy_rows.items()} }")

    verdicts.set_conn(None)
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print(f"JUDGE DRYRUN FAIL {failed}")
        return 1
    print(f"JUDGE DRYRUN PASS ({len(RESULTS)}/{len(RESULTS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
