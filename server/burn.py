"""burn.py — burn-таблица по каналам: телеметрия + бюджеты + пробы квот.

Числа только фактические: отсутствие данных → 0/None, НЕ оценка. Каналы без
вызовов присутствуют с нулями — молчание канала тоже сигнал. Лимит 0 в
конфиге означает «без бюджета» → budget_limit=None.
"""
from __future__ import annotations

from . import quotas, telemetry
from .config import load_config
from .db import get_db, today_local

_ZERO = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
         "cache_read_tokens": 0, "escalations": 0, "failures": 0,
         "filter_burned_tokens": 0, "cost_usd": 0, "total_ms": 0}


def table(days: int = 1) -> list[dict]:
    cfg = load_config()
    rows = {r["channel"]: {k: (v if v is not None else 0) for k, v in r.items()}
            for r in telemetry.burn_rows(days)}
    quota = {q["channel"]: q for q in quotas.latest()}
    budget_rows = {r["channel"]: dict(r) for r in get_db().execute(
        "SELECT * FROM budgets WHERE day=?", (today_local(),)).fetchall()}
    # сначала каналы конфига, затем встреченные в телеметрии, но не в конфиге
    names = list(dict.fromkeys(list(cfg.channels) + list(rows)))
    by_class: dict[str, list] = {}
    for r in telemetry.burn_rows_by_class(days):
        by_class.setdefault(r["channel"], []).append(
            {k: (v if v is not None else 0) for k, v in r.items() if k != "channel"})
    tier_calls = telemetry.tier_calls_today()
    out = []
    for name in names:
        ch = cfg.channel(name)
        t = rows.get(name) or {"channel": name, **_ZERO}
        b = budget_rows.get(name, {})
        q = quota.get(name)
        limit = ch.daily_budget_tokens if ch and ch.daily_budget_tokens else None
        out.append({**t,
                    "spent_tokens": b.get("spent_tokens", 0),
                    "spent_calls": b.get("spent_calls", 0),
                    "budget_limit": limit,
                    "quota_ok": q["ok"] if q else None,
                    "quota_remaining": q["remaining"] if q else None,
                    "quota_unit": q["unit"] if q else None,
                    "quota_ts": q["ts"] if q else None,
                    "by_class": by_class.get(name, []),
                    "tier_calls": tier_calls})
    return out


def render_text(days: int = 1) -> str:
    """Компактная текстовая таблица для TG/CLI; null → «—»."""
    rows = table(days)
    head = ("канал", "вызовы", "prompt", "completion", "reasoning", "кэш",
            "эск", "провалы", "фильтр", "бюджет")

    def budget(r: dict) -> str:
        if r["budget_limit"] is None and not r["spent_tokens"]:
            return "—"
        lim = "—" if r["budget_limit"] is None else str(r["budget_limit"])
        return f"{r['spent_tokens']}/{lim}"

    lines = [" | ".join(head), "-+-".join("-" * len(h) for h in head)]
    for r in rows:
        lines.append(" | ".join([
            str(r["channel"]), str(r["calls"]), str(r["prompt_tokens"]),
            str(r["completion_tokens"]), str(r["reasoning_tokens"]),
            str(r["cache_read_tokens"]), str(r["escalations"]),
            str(r["failures"]), str(r["filter_burned_tokens"]), budget(r)]))
    return "\n".join(lines)
