"""antiloop.py — предохранители против циклов и пережога (урок Paperclip: 1000 ранов, 92% fail).

Три контура:
 - дневной бюджет канала в токенах (атомарные счётчики в SQLite, не state.json —
   json расится под конкурентной записью);
 - fingerprint повторяющегося входа: N провалов подряд с тем же входом → стоп;
 - treading-water: сессия ходит по кругу «провал → повтор» без прогресса.

Алерты шлются через alerts.py лениво: модуль пишется параллельно, его
отсутствие не должно ломать роутер — fallback в stderr.
"""
from __future__ import annotations

import hashlib
import sys

from .config import load_config
from .db import get_db, now_iso, today_local


def _alert(severity: str, kind: str, text: str, meta: dict | None = None) -> int:
    try:
        from . import alerts
        return alerts.send(severity, kind, text, meta)
    except ImportError:
        print(f"noosphere-alert[{severity}/{kind}]: {text}", file=sys.stderr)
        return 0
    except Exception as e:  # сломанный контур реакции не должен ронять вызовы LLM
        print(f"noosphere-alert: send упал ({e!r}); текст: {text}", file=sys.stderr)
        return 0


# ── бюджеты каналов ───────────────────────────────────────────────────────────
def note_spend(channel: str, tokens: int, calls: int = 1) -> tuple[int, int]:
    """Атомарный инкремент дневного счётчика канала. Возвращает (spent_tokens, spent_calls)."""
    db = get_db()
    day = today_local()
    db.execute("INSERT OR IGNORE INTO budgets(day, channel) VALUES(?, ?)", (day, channel))
    db.execute("UPDATE budgets SET spent_tokens = spent_tokens + ?, spent_calls = spent_calls + ? "
               "WHERE day = ? AND channel = ?", (int(tokens), int(calls), day, channel))
    db.commit()
    row = db.execute("SELECT spent_tokens, spent_calls FROM budgets WHERE day = ? AND channel = ?",
                     (day, channel)).fetchone()
    return row["spent_tokens"], row["spent_calls"]


def check_budget(channel: str) -> tuple[bool, str]:
    """Разрешён ли расход по каналу. daily_budget_tokens=0 → бюджета нет.
    Пороги warn/crit — один алерт на порог на день (флаги warned70/warned90),
    иначе каждый вызов за порогом спамил бы контур реакции."""
    cfg = load_config()
    ch = cfg.channel(channel)
    budget = ch.daily_budget_tokens if ch else 0
    if not budget:
        return True, "ok"
    db = get_db()
    day = today_local()
    db.execute("INSERT OR IGNORE INTO budgets(day, channel) VALUES(?, ?)", (day, channel))
    db.commit()
    row = db.execute("SELECT spent_tokens, warned70, warned90 FROM budgets "
                     "WHERE day = ? AND channel = ?", (day, channel)).fetchone()
    pct = row["spent_tokens"] * 100 / budget
    an = cfg.antiloop
    if pct >= an.budget_crit_pct and not row["warned90"]:
        db.execute("UPDATE budgets SET warned90 = 1 WHERE day = ? AND channel = ?", (day, channel))
        db.commit()
        _alert("crit", "budget", f"канал {channel}: {pct:.0f}% дневного бюджета "
               f"({row['spent_tokens']}/{budget} токенов)",
               {"channel": channel, "spent": row["spent_tokens"], "budget": budget})
    elif pct >= an.budget_warn_pct and not row["warned70"]:
        db.execute("UPDATE budgets SET warned70 = 1 WHERE day = ? AND channel = ?", (day, channel))
        db.commit()
        _alert("warning", "budget", f"канал {channel}: {pct:.0f}% дневного бюджета "
               f"({row['spent_tokens']}/{budget} токенов)",
               {"channel": channel, "spent": row["spent_tokens"], "budget": budget})
    if pct >= 100:
        return False, "exhausted"
    if pct >= an.budget_crit_pct:
        return True, "crit"
    if pct >= an.budget_warn_pct:
        return True, "warn"
    return True, "ok"


# ── fingerprint повторов ──────────────────────────────────────────────────────
def fingerprint_of(model: str, prompt: str) -> str:
    """sha8 входа: одинаковый промпт на той же модели → одинаковый ключ."""
    return hashlib.sha256((model + "\n" + prompt).encode("utf-8")).hexdigest()[:8]


def note_attempt(fingerprint: str, ok: bool) -> tuple[bool, int]:
    """Учёт попытки с данным входом. Успех сбрасывает счётчик (вход «расклеился»).
    stop=True, когда провалов подряд ≥ max_repeat_fingerprint."""
    db = get_db()
    day = today_local()
    db.execute("INSERT OR IGNORE INTO fingerprints(day, fingerprint) VALUES(?, ?)",
               (day, fingerprint))
    if ok:
        db.execute("UPDATE fingerprints SET count = 0, last_ts = ? WHERE day = ? AND fingerprint = ?",
                   (now_iso(), day, fingerprint))
        db.commit()
        return False, 0
    db.execute("UPDATE fingerprints SET count = count + 1, last_ts = ? WHERE day = ? AND fingerprint = ?",
               (now_iso(), day, fingerprint))
    db.commit()
    row = db.execute("SELECT count FROM fingerprints WHERE day = ? AND fingerprint = ?",
                     (day, fingerprint)).fetchone()
    count = row["count"]
    return count >= load_config().antiloop.max_repeat_fingerprint, count


# ── treading-water ────────────────────────────────────────────────────────────
def treading_water(session_id: str) -> bool:
    """Сессия топчется: последние max_water_cycles ходов подряд — провалы (ok=0).
    Эвристика сознательно грубая: точное «чередование без прогресса» неотличимо
    от честной эскалации, а серия чистых провалов — всегда сигнал стопа."""
    if not session_id:
        return False
    n = load_config().antiloop.max_water_cycles
    rows = get_db().execute(
        "SELECT ok FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, n)).fetchall()
    return len(rows) >= n and all(r["ok"] == 0 for r in rows)
