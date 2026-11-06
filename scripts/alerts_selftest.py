"""Selftest контура реакции: узкий dead-letter (crit 'new' без delivery_error).

Проверяет: свежий crit не трогаем; crit старше _NEW_STALE_MIN без следа доставки
получает delivery_error и уходит в ретрай; исчерпанный кап пишет fallback-лог и
строку alerting-broken; успешная доставка переводит в sent.
Запуск из корня noosphere: python scripts/alerts_selftest.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import alerts, db  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"CHECK {name} {detail} ok={ok}")
    return ok


def aged(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> int:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_schema(conn)
    db.get_db = lambda: conn
    alerts.get_db = lambda: conn  # alerts импортирует get_db напрямую (from-import)
    tmp = Path(tempfile.mkdtemp(prefix="noosphere-alerts-"))
    cfg = alerts.load_config()
    cfg.data_dir = tmp

    sent: list[str] = []

    def _fake_dispatch(text: str):
        sent.append(text)
        return True, ""

    alerts._dispatch = _fake_dispatch

    conn.execute("INSERT INTO alerts(ts, kind, severity, text, status, meta)"
                 " VALUES(?,?,?,?,'new','{}')", (aged(1), "fresh", "crit", "свежий"))
    conn.execute("INSERT INTO alerts(ts, kind, severity, text, status, meta)"
                 " VALUES(?,?,?,?,'new','{}')", (aged(30), "lost", "crit", "потерянный"))
    conn.execute("INSERT INTO alerts(ts, kind, severity, text, status, meta)"
                 " VALUES(?,?,?,?,'new',?)", (aged(30), "capped", "crit", "кап",
                                              '{"delivery_error":"x","delivery_attempts":6}'))
    conn.commit()

    n = alerts.check_sla()
    rows = {r["kind"]: r for r in conn.execute("SELECT * FROM alerts")}
    check("fresh_untouched", rows["fresh"]["status"] == "new"
          and not (rows["fresh"]["meta"] or "").count("delivery_error"),
          f"status={rows['fresh']['status']}")
    check("lost_retried", rows["lost"]["status"] == "sent" and n >= 1,
          f"status={rows['lost']['status']} sent={len(sent)}")
    check("cap_fallback", (tmp / "alerts-fallback.log").exists()
          and rows["capped"]["meta"].count("transport_broken")
          and any(r["kind"] == "alerting-broken"
                  for r in conn.execute("SELECT kind FROM alerts")),
          f"fallback={(tmp / 'alerts-fallback.log').exists()}")

    db.get_db = None  # type: ignore[assignment]
    failed = [name for name, ok, _ in RESULTS if not ok]
    if failed:
        print(f"ALERTS SELFTEST FAIL {failed}")
        return 1
    print(f"ALERTS SELFTEST PASS ({len(RESULTS)}/{len(RESULTS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
