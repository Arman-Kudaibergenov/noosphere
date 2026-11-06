"""run_quality.py — ночной анализатор маршрутизации и качества ранов (батч 6).

Раз в сутки (час route.analysis_hour, локальный) придирчиво разбирает route_log
и runs/turns за последнюю неделю и шлёт дайджест владельцу (warning →
alerts.flush_digest). АВТО-ПРИМЕНЕНИЯ НЕТ — только предложения.

Предохранитель качества: предложение «сэкономить» (понизить тир) выносится,
только если по фактическим данным нижний тир на похожей работе держит
success-rate не хуже; иначе — «не экономить». Данных мало — предложений нет.
"""
from __future__ import annotations

import datetime as dt
import statistics

from .config import load_config
from .db import get_db, now_iso, today_local

MIN_SAMPLE = 5          # меньше — никаких предложений, только факты
REWORK_WINDOW_MIN = 30  # новый ход/ран вскоре после done = переделка


def _cutoffs(days: int) -> tuple[str, str]:
    """(cutoff_ts_utc_iso, cutoff_day_local) для окна анализа."""
    ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    tz = dt.timezone(dt.timedelta(hours=load_config().server.tz_offset_hours))
    day = (dt.datetime.now(tz) - dt.timedelta(days=days)).date().isoformat()
    return ts, day


def _median(xs: list[float]) -> float:
    return round(statistics.median(xs), 4) if xs else 0.0


def _classifier_stats(day_cut: str) -> dict:
    db = get_db()
    rows = db.execute(
        "SELECT prefilter, err, override FROM route_log WHERE day >= ?", (day_cut,)).fetchall()
    total = len(rows)
    if not total:
        return {"total": 0}
    prefilter = sum(1 for r in rows if r["prefilter"])
    errs = sum(1 for r in rows if r["err"])
    overrides = [r for r in rows if r["override"]]
    # топ путаниц: проект классификатора → проект, на который переключил владелец
    confusions: dict[str, int] = {}
    for r in db.execute(
            "SELECT rl.project_name AS was, p.name AS became, COUNT(*) AS n"
            " FROM route_log rl LEFT JOIN projects p ON p.id = rl.override_project_id"
            " WHERE rl.override = 1 AND rl.override_project_id IS NOT NULL"
            " AND rl.day >= ? GROUP BY was, became ORDER BY n DESC", (day_cut,)):
        confusions[f"{r['was'] or '?'}→{r['became'] or '?'}"] = r["n"]
    return {"total": total, "prefilter_pct": round(100 * prefilter / total),
            "llm_err_pct": round(100 * errs / total), "overrides": len(overrides),
            "confusions": confusions}


def _run_stats(ts_cut: str) -> dict:
    """Качество ранов по проектам (папка сессии) и тирам ходов прогона."""
    db = get_db()
    runs = db.execute(
        "SELECT r.id, r.session_id, r.status, r.created_at, r.finished_at,"
        " COALESCE(s.folder, 'без проекта') AS project"
        " FROM runs r LEFT JOIN sessions s ON s.id = r.session_id"
        " WHERE r.created_at >= ?", (ts_cut,)).fetchall()
    if not runs:
        return {"total": 0}
    by_project: dict[str, dict] = {}
    rework = 0
    done = 0
    for r in runs:
        p = by_project.setdefault(r["project"], {"total": 0, "error": 0, "canceled": 0,
                                                 "costs": [], "ms": [], "esc": 0, "turns": 0})
        p["total"] += 1
        p["error"] += r["status"] == "error"
        p["canceled"] += r["status"] == "canceled"
        trow = db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost,"
            " COALESCE(SUM(ms),0) AS ms, COALESCE(SUM(escalated),0) AS esc"
            " FROM turns WHERE session_id = ? AND task_class != 'route'",
            (r["session_id"],)).fetchone()
        if trow and trow["n"]:
            p["turns"] += trow["n"]
            p["costs"].append(trow["cost"])
            p["ms"].append(trow["ms"])
            p["esc"] += trow["esc"]
        if r["status"] == "done" and r["finished_at"]:
            done += 1
            try:
                fin = dt.datetime.fromisoformat(r["finished_at"].replace("Z", "+00:00"))
            except ValueError:
                continue
            till = (fin + dt.timedelta(minutes=REWORK_WINDOW_MIN)) \
                .isoformat(timespec="seconds").replace("+00:00", "Z")
            # сигнал неудовлетворённости: новый ход в той же сессии или новый ран
            # того же проекта в течение получаса после завершения
            if db.execute(
                    "SELECT 1 FROM session_turns WHERE session_id = ?"
                    " AND ts > ? AND ts <= ? LIMIT 1",
                    (r["session_id"], r["finished_at"], till)).fetchone():
                rework += 1
            elif db.execute(
                    "SELECT 1 FROM runs r2 JOIN sessions s2 ON s2.id = r2.session_id"
                    " WHERE r2.id != ? AND COALESCE(s2.folder,'без проекта') = ?"
                    " AND r2.created_at > ? AND r2.created_at <= ? LIMIT 1",
                    (r["id"], r["project"], r["finished_at"], till)).fetchone():
                rework += 1
    projects_out = {}
    for name, p in sorted(by_project.items()):
        projects_out[name] = {
            "total": p["total"], "error_pct": round(100 * p["error"] / p["total"]),
            "canceled_pct": round(100 * p["canceled"] / p["total"]),
            "median_cost_usd": _median(p["costs"]), "median_ms": int(_median(p["ms"])),
            "escalated_pct": round(100 * p["esc"] / p["turns"]) if p["turns"] else 0}
    return {"total": len(runs), "done": done, "rework": rework,
            "rework_pct": round(100 * rework / done) if done else 0,
            "projects": projects_out}


def _tier_suggestion(ts_cut: str) -> str | None:
    """Предохранитель качества: «сэкономить» предлагаем, только если нижний
    тир по факту держит success-rate не хуже на сопоставимом объёме данных."""
    db = get_db()
    rows = db.execute(
        "SELECT tier_used, COUNT(*) AS n, SUM(ok) AS ok_n,"
        " COALESCE(SUM(cost_usd),0) AS cost FROM turns"
        " WHERE ts >= ? AND task_class != 'route' AND tier_used != ''"
        " GROUP BY tier_used", (ts_cut,)).fetchall()
    tiers = {r["tier_used"]: r for r in rows if r["n"] >= MIN_SAMPLE}
    ladder = [t for t in ("local", "base", "mid", "deep", "max") if t in tiers]
    for i in range(len(ladder) - 1):
        low, high = tiers[ladder[i]], tiers[ladder[i + 1]]
        low_rate, high_rate = low["ok_n"] / low["n"], high["ok_n"] / high["n"]
        if low_rate >= high_rate and low["cost"] < high["cost"]:
            return (f"кандидат на экономию: тир {ladder[i + 1]} → {ladder[i]} "
                    f"(success {round(100 * low_rate)}% ≥ {round(100 * high_rate)}%, "
                    f"n={low['n']}/{high['n']}; стоимость "
                    f"${low['cost']:.3f} < ${high['cost']:.3f})")
    if ladder:
        worst = max(ladder[1:] or ladder[:1], key=lambda t: 1 - tiers[t]["ok_n"] / tiers[t]["n"])
        w = tiers[worst]
        if w["ok_n"] / w["n"] < 0.8:
            return (f"НЕ экономить: тир {worst} и так даёт success "
                    f"{round(100 * w['ok_n'] / w['n'])}% (n={w['n']}) — понижение "
                    "без данных, что нижний тир справится, запрещено")
    return None


def run_analysis(days: int = 7) -> str:
    """Полный разбор окна → текст дайджеста. Не бросает исключений по данным."""
    ts_cut, day_cut = _cutoffs(days)
    cls = _classifier_stats(day_cut)
    rs = _run_stats(ts_cut)
    parts = [f"[route_analysis] {days}д:"]
    if cls["total"]:
        conf = ", ".join(f"{k} ×{v}" for k, v in
                         list(cls["confusions"].items())[:3]) or "нет"
        parts.append(
            f"классификаций {cls['total']} (prefilter {cls['prefilter_pct']}%, "
            f"ошибок LLM {cls['llm_err_pct']}%, overrides {cls['overrides']}: {conf})")
    else:
        parts.append("классификаций нет")
    if rs["total"]:
        parts.append(f"ранов {rs['total']} (rework {rs['rework_pct']}% из done={rs['done']})")
        for name, p in rs["projects"].items():
            parts.append(
                f"  {name}: {p['total']} (err {p['error_pct']}%, cancel {p['canceled_pct']}%, "
                f"медиана ${p['median_cost_usd']}, {p['median_ms']} мс, esc {p['escalated_pct']}%)")
    else:
        parts.append("ранов нет")
    suggestion = _tier_suggestion(ts_cut) if (cls["total"] >= MIN_SAMPLE
                                              or rs["total"] >= MIN_SAMPLE) else None
    if suggestion:
        parts.append(f"Предложение: {suggestion}")
    else:
        n = max(cls["total"], rs["total"])
        parts.append(f"данных мало ({n}) или качество нижнего тира недоказано — предложений нет")
    return "\n".join(parts)


def should_run_today() -> bool:
    """True, если анализ за сегодня ещё не выполнялся и час настал."""
    cfg = load_config()
    db = get_db()
    row = db.execute("SELECT value FROM meta WHERE key='last_route_analysis'").fetchone()
    if row and row[0] == today_local():
        return False
    tz = dt.timezone(dt.timedelta(hours=cfg.server.tz_offset_hours))
    return dt.datetime.now(tz).hour >= cfg.route.analysis_hour


def mark_run_today() -> None:
    db = get_db()
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_route_analysis', ?)",
               (today_local(),))
    db.commit()
