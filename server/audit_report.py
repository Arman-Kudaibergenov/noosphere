"""Недельный HTML-отчёт аудита успешности чатов (серверный рендер, 0 LLM-токенов).

`GET /v1/audit/report?days=7` отдаёт самодостаточную страницу: KPI недели, разрез по
классам задач, топ причин fail по сигналам правил, переделки и незакрытые петли,
история контрактов оптимизации и цена аудита (токены судьи). Читает только свои
таблицы аудита и telemetry; секреты не выводятся никогда.
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from . import db, harness_usage, verdicts

CSS = """
body{background:#EDF1EA;color:#1B241F;font:14px/1.55 "Golos Text","Segoe UI",system-ui,sans-serif;
margin:0;padding:32px 18px 60px}
.w{max-width:1000px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:#5A675F;font:12px/1.6 "JetBrains Mono",Consolas,monospace;margin-bottom:22px}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:22px}
.kpi{background:#fff;border:1px solid rgba(27,36,31,.12);border-radius:10px;padding:10px 16px;min-width:130px}
.kpi b{display:block;font-size:20px;font-weight:650}
.kpi span{color:#5A675F;font:11px/1.4 "JetBrains Mono",Consolas,monospace}
section{background:#fff;border:1px solid rgba(27,36,31,.12);border-radius:12px;padding:16px 18px;margin-bottom:16px}
h2{font:600 12px/1 "JetBrains Mono",Consolas,monospace;text-transform:uppercase;
letter-spacing:1.2px;color:#155D4B;margin:0 0 12px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid rgba(27,36,31,.09);vertical-align:top}
th{font:600 11px/1.4 "JetBrains Mono",Consolas,monospace;color:#5A675F;text-transform:uppercase}
tr:nth-child(even) td{background:rgba(244,247,241,.7)}
.st{display:inline-block;padding:1px 8px;border-radius:999px;font:600 10px/1.7 "JetBrains Mono",Consolas,monospace}
.st-success{background:rgba(21,93,75,.10);color:#155D4B;border:1px solid #155D4B}
.st-partial{background:rgba(252,243,215,.9);color:#7A5410;border:1px solid #7A5410}
.st-fail{background:rgba(247,228,224,.8);color:#8F352C;border:1px solid #8F352C}
.st-unresolved,.st-pending{background:rgba(61,74,66,.08);color:#3D4A42;border:1px solid #3D4A42}
.num{text-align:right;font-variant-numeric:tabular-nums}
footer{color:#5A675F;font:11px/1.8 "JetBrains Mono",Consolas,monospace}
"""


def _esc(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")


def build_html(days: int = 7) -> str:
    c = db.get_db()
    since = _days_ago(days)
    rows = c.execute(
        "SELECT v.*, COALESCE(s.meta,'') AS session_meta FROM chat_verdict v"
        " LEFT JOIN sessions s ON s.id = v.session_id WHERE v.ts >= ?"
        " ORDER BY v.ts DESC", (since,)).fetchall()
    # BACKLOG-15 п.5: пробные сессии не участвуют в качестве — иначе доля success
    # считается по нагрузочным «посчитай до 350» и прибору нельзя верить
    chats = [r for r in rows if not verdicts.is_trial_meta(r["session_meta"])]
    stats = {s: 0 for s in ("success", "partial", "fail", "unresolved")}
    for ch in chats:
        stats[ch["status"]] = stats.get(ch["status"], 0) + 1
    judge_tokens = sum(ch["tokens"] or 0 for ch in chats)
    cost = c.execute("SELECT SUM(cost_usd) s, SUM(prompt_tokens+completion_tokens) t"
                     " FROM turns WHERE ts >= ? AND COALESCE(turn_kind,'') != 'judge'",
                     (since,)).fetchone()
    classes = c.execute(
        "SELECT t.task_class cls, COUNT(*) n,"
        " SUM(CASE WHEN v.verdict='success' THEN 1 ELSE 0 END) ok,"
        " SUM(t.prompt_tokens+t.completion_tokens) tok"
        " FROM turns t LEFT JOIN turn_verdict v ON v.session_id=t.session_id"
        "  AND v.rules_version=? WHERE t.ts >= ? AND COALESCE(t.turn_kind,'') != 'judge'"
        " GROUP BY t.task_class ORDER BY n DESC", (verdicts.RULES_VERSION, since)).fetchall()
    fails = c.execute(
        "SELECT rule, COUNT(*) n FROM verdict_signals WHERE ts >= ? GROUP BY rule"
        " ORDER BY n DESC LIMIT 8", (since,)).fetchall()
    contracts = c.execute(
        "SELECT id, ts, target, risk, status, diff_json, evidence_json FROM audit_contracts"
        " WHERE ts >= ? ORDER BY id DESC", (since,)).fetchall()
    judged = sum(v for k, v in stats.items() if k != "unresolved")
    rate = round(100.0 * stats["success"] / judged, 1) if judged else 0.0
    price = round((cost["s"] or 0) / stats["success"], 4) if stats["success"] else None
    varied = sum(1 for ch in chats
                 if ch["soft_status"] and ch["soft_status"] != ch["status"])

    rows_chats = "".join(
        f"<tr><td>{_esc(ch['session_id'][:12])}</td>"
        f"<td><span class='st st-{_esc(ch['status'])}'>{_esc(ch['status'])}</span></td>"
        f"<td>{_esc(ch['soft_status'] or '—')}</td>"
        f"<td>{_esc(ch['reason'])}</td><td class='num'>{ch['tokens'] or 0}</td>"
        f"<td>{_esc((ch['ts'] or '')[:16])}</td></tr>" for ch in chats[:40]) or \
        "<tr><td colspan=6>за период закрытых чатов с вердиктом нет</td></tr>"
    rows_cls = "".join(
        f"<tr><td>{_esc(r['cls'] or 'chat')}</td><td class='num'>{r['n']}</td>"
        f"<td class='num'>{r['ok'] or 0}</td>"
        f"<td class='num'>{round(100.0 * (r['ok'] or 0) / r['n'], 1) if r['n'] else 0}%</td>"
        f"<td class='num'>{r['tok'] or 0}</td></tr>" for r in classes) or \
        "<tr><td colspan=5>нет ходов за период</td></tr>"
    rows_fail = "".join(f"<tr><td>{_esc(r['rule'])}</td><td class='num'>{r['n']}</td></tr>"
                        for r in fails) or "<tr><td colspan=2>сигналов fail нет</td></tr>"
    rows_con = "".join(
        f"<tr><td>#{r['id']}</td><td>{_esc(r['target'])}</td><td>{_esc(r['risk'])}</td>"
        f"<td><span class='st st-{_esc(r['status'])}'>{_esc(r['status'])}</span></td>"
        f"<td>{_esc(r['diff_json'])}</td></tr>" for r in contracts) or \
        "<tr><td colspan=5>контрактов за период нет</td></tr>"
    har = harness_usage.summary(days)
    rows_har = "".join(
        f"<tr><td>{_esc(r['source'])}</td><td>{_esc(r['day'])}</td>"
        f"<td class='num'>{r['sessions']}</td><td class='num'>{r['reqs'] or 0}</td>"
        f"<td class='num'>{(r['inp'] or 0) - (r['cached'] or 0)}</td>"
        f"<td class='num'>{r['cached'] or 0}</td><td class='num'>{r['outp'] or 0}</td>"
        f"<td class='num'>+{r['added'] or 0}/-{r['removed'] or 0}</td></tr>" for r in har) or \
        "<tr><td colspan=8>ингеста харнесов за период нет (scripts/ingest_harness_usage.py)</td></tr>"

    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ноосфера · аудит чатов за {days} дн.</title><style>{CSS}</style></head>
<body><div class="w">
<h1>Аудит успешности чатов · {days} дн.</h1>
<div class="sub">сгенерировано сервером :8300 · {datetime.now(timezone.utc).isoformat(timespec='minutes')}Z ·
правила v{verdicts.RULES_VERSION} · судья и оптимизатор без авто-применения</div>
<div class="kpis">
  <div class="kpi"><b>{judged}</b><span>чатов оценено</span></div>
  <div class="kpi"><b>{rate}%</b><span>success-rate</span></div>
  <div class="kpi"><b>{stats['fail']}</b><span>fail</span></div>
  <div class="kpi"><b>{stats['partial']}</b><span>partial</span></div>
  <div class="kpi"><b>{price if price is not None else '—'}</b><span>$ на успешную задачу</span></div>
  <div class="kpi"><b>{judge_tokens}</b><span>токенов судьи</span></div>
  <div class="kpi"><b>{varied}</b><span>жёсткий ≠ мягкий</span></div>
  <div class="kpi"><b>{round(cost['s'] or 0, 3)}</b><span>$ всего за период</span></div>
</div>
<section><h2>Чаты и финальные статусы (жёсткий — основа, мягкий — вариативность)</h2>
<table><thead><tr><th>сессия</th><th>статус</th><th>мягкий</th><th>причина судьи</th><th>токены</th><th>когда</th></tr></thead>
<tbody>{rows_chats}</tbody></table></section>
<section><h2>Разрез по классам задач (ходы, правила v{verdicts.RULES_VERSION})</h2>
<table><thead><tr><th>класс</th><th>ходов</th><th>success</th><th>доля</th><th>токенов</th></tr></thead>
<tbody>{rows_cls}</tbody></table></section>
<section><h2>Топ причин fail (сигналы правил)</h2>
<table><thead><tr><th>правило</th><th>сколько</th></tr></thead><tbody>{rows_fail}</tbody></table></section>
<section><h2>Контракты оптимизации</h2>
<table><thead><tr><th>id</th><th>цель</th><th>риск</th><th>статус</th><th>diff</th></tr></thead>
<tbody>{rows_con}</tbody></table></section>
<section><h2>Второй контур: сессии харнесов (не смешивается с цифрами Ноосферы)</h2>
<table><thead><tr><th>source</th><th>день</th><th>сессий</th><th>запросов</th>
<th>некэш. ввод</th><th>кэш</th><th>вывод</th><th>строк кода</th></tr></thead>
<tbody>{rows_har}</tbody></table></section>
<footer>Источник: chat_verdict, turn_verdict, verdict_signals, audit_contracts, turns.
Секреты в отчёт не выводятся. Утверждение контрактов — POST /v1/audit/contracts/&lt;id&gt;/approve.</footer>
</div></body></html>"""
