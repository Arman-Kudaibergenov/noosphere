"""Сжатый транскрипт чата для судьи (chat_judge): ходы без системных промптов,
tool-след из tool_audit, попытки из turns, метки владельца и признаки правил
приложены.

Секреты не проезжают дважды: снапшоты ходов уже маскируются при записи
(`sessions._mask_snapshot`), чтение идёт через санкционированный
`sessions.get_history`; здесь маска применяется ещё раз, دفاعно, к любому тексту
(Bearer, длинные hex/base64, пути .secrets/.ssh) — судья не должен видеть секретов
даже случайно вставленных в промпт.
"""
from __future__ import annotations

import json
import re

from . import verdicts

MASK_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.I),
    re.compile(r"\b[0-9a-fA-F]{40,}\b"),
    re.compile(r"\b[A-Za-z0-9_\-./+=]{56,}\b"),
    re.compile(r"(?:~|/?\w+:)?[\\/](?:\.secrets|\.ssh)[\\/]\S+"),
)
REDACT = "***"


def mask(text: str) -> str:
    out = text or ""
    for pat in MASK_PATTERNS:
        out = pat.sub(REDACT, out)
    return out


def _clip(text: str, cap: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[:cap] + " …"


def condensed(session_id: str, conn=None, max_turns: int = 30,
              prompt_cap: int = 1200, answer_cap: int = 1500) -> tuple[str, dict]:
    """(текст транскрипта, статистика). Пустой текст = судить нечего."""
    c = conn if conn is not None else verdicts._c()
    blocks: list[str] = []
    # та же разборка снапшотов, что в sessions.get_history, но на переданной
    # коннекции: слой остаётся тестируемым на своей sqlite без боевой БД
    rows = c.execute(
        "SELECT snapshot FROM session_turns WHERE session_id=? AND status='done'"
        " ORDER BY turn_no DESC LIMIT ?", (session_id, max_turns)).fetchall()
    msgs: list[dict] = []
    for row in reversed(rows):
        try:
            snap = json.loads(row["snapshot"] or "{}")
        except ValueError:
            continue
        prompt = snap.get("prompt")
        if not prompt:
            if snap.get("kind") == "system-note" and snap.get("text"):
                msgs.append({"role": "assistant", "content": snap["text"]})
            continue
        msgs.append({"role": "user", "content": prompt})
        text = snap.get("text") or ""
        if not text:
            text = f"[ход завершился ошибкой: {snap.get('err') or 'неизвестная ошибка'}]"
        msgs.append({"role": "assistant", "content": text})
    n_user = n_assistant = 0
    for m in msgs:
        role = str(m.get("role") or "")
        if role == "system":
            continue
        content = mask(str(m.get("content") or ""))
        if not content.strip():
            continue
        if role == "user":
            n_user += 1
            blocks.append(f"[ход {n_user}] ПОЛЬЗОВАТЕЛЬ: {_clip(content, prompt_cap)}")
        elif role == "assistant":
            n_assistant += 1
            blocks.append(f"[ход {n_assistant}] АССИСТЕНТ: {_clip(content, answer_cap)}")

    tools = c.execute(
        "SELECT turn_no, tool, rc, note, args_summary, out_len FROM tool_audit"
        " WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
    for t in tools:
        blocks.append(f"[инструмент ход {t['turn_no']}] {t['tool']} rc={t['rc']}"
                      f" note={t['note'] or '-'} out={t['out_len'] or 0}B"
                      f" args={_clip(mask(t['args_summary'] or ''), 160)}")

    attempts = c.execute(
        "SELECT ts, tier_start, tier_used, model, finish, err, escalated,"
        " content_filter, prompt_tokens, completion_tokens FROM turns"
        " WHERE session_id=? ORDER BY ts", (session_id,)).fetchall()
    for i, a in enumerate(attempts, 1):
        blocks.append(f"[вызов {i}] {a['tier_start']}→{a['tier_used']} {a['model']}"
                      f" finish={a['finish'] or '-'} err={_clip(a['err'] or '-', 100)}"
                      f" escalated={a['escalated']} filter={a['content_filter']}"
                      f" tokens={a['prompt_tokens']}+{a['completion_tokens']}")

    eff = verdicts.owner_effect(session_id, c)
    marks = verdicts.owner_marks(session_id, c, 3)
    if marks:
        journal = "; ".join(
            f"{m['mark']} {m['ts']}"
            + (f" «{_clip(mask(m['note']), 120)}»" if m["note"] else "")
            for m in marks)
        blocks.append("[метки владельца] " + journal)

    proxy, signals = verdicts.chat_proxy_verdict(session_id, c)
    blocks.append("[признаки правил] proxy=" + proxy + " signals="
                  + json.dumps(signals, ensure_ascii=False)[:1500])

    text = "\n".join(blocks)
    return text, {"turns": max(n_user, n_assistant), "user": n_user,
                  "assistant": n_assistant, "tools": len(tools),
                  "attempts": len(attempts), "chars": len(text), "proxy": proxy,
                  "owner_mark": eff.get("mark", ""), "owner_effect": eff}
