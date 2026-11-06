#!/usr/bin/env python3
"""probe_prefix.py — живая проба префикса чат-хода: токены и поведение.

Собирает системный блок РОВНО как main._run_turn_inner для интерактивного хода с
инструментами (агентский блок + правила рабочего места + список MCP + память
разговоров; префлайт — в промпт; JSON инструментов — параметром tools) и делает
один настоящий вызов модели ролью `chat`. Правила и память берутся из КОПИИ
боевой БД, поэтому число сравнимо с боевым замером; боевая БД на запись не
открывается, алерты пробы уходят в log, копия удаляется.

Инструменты НЕ исполняются: вызов записывается и получает отказ, поэтому пробу
«а поставит ли агент devrun_request» можно гонять без последствий для контура.

Прогон:  ~/noosphere/.venv/bin/python scripts/probe_prefix.py [ключи]
  --code <дерево>     мерить код другого дерева (сравнение «до/после» без деплоя);
                      по умолчанию — дерево этого скрипта
  --prompt <текст>    свой промт (по умолчанию — короткий замер префикса)
  --max-tokens <N>    потолок ответа, по умолчанию 256: станции нужен запас
                      ~100 токенов на reasoning, иначе пустой ответ и эскалация
  --iters <N>         потолок итераций агентского цикла, по умолчанию 1 (замер
                      токенов); для пробы поведения нужно 4-6 — модель сначала
                      ходит в разведку и только потом ставит задачу
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
LIVE = Path(os.path.expanduser("~/noosphere"))
PROBE_PROMPT = "Проба размера префикса. Ответь одним словом: ОК"
CALLS: list[dict] = []


def _opt(flag: str, default: str) -> str:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def prepare(code_tree: str) -> str:
    """Копия боевого конфига (data_dir и алерты правятся) и снимок боевой БД."""
    tmp = tempfile.mkdtemp(prefix="ns-probe-")
    src = (LIVE / "config.yaml").read_text(encoding="utf-8")
    if re.search(r"(?m)^data_dir:.*$", src):
        src = re.sub(r"(?m)^data_dir:.*$", "data_dir: %s" % json.dumps(tmp), src)
    else:
        src += "\ndata_dir: %s\n" % json.dumps(tmp)
    src = re.sub(r"(?m)^(\s+)transport:.*$", r"\1transport: log", src)
    (Path(tmp) / "config.yaml").write_text(src, encoding="utf-8")
    os.environ["NOOSPHERE_CONFIG"] = str(Path(tmp) / "config.yaml")
    con = sqlite3.connect(f"file:{LIVE / 'data' / 'noosphere.db'}?mode=ro", uri=True)
    dst = sqlite3.connect(str(Path(tmp) / "noosphere.db"))
    with dst:
        con.backup(dst)
    con.close()
    dst.close()
    sys.path.insert(0, str(Path(code_tree).expanduser().resolve()))
    return tmp


async def _record(tool_call: dict) -> str:
    fn = (tool_call or {}).get("function") or {}
    name = fn.get("name") or (tool_call or {}).get("name") or "?"
    raw = fn.get("arguments")
    if isinstance(raw, str):
        try:
            args = json.loads(raw)
        except Exception:                                     # noqa: BLE001
            args = {"_raw": raw[:200]}
    else:
        args = raw if isinstance(raw, dict) else {}
    CALLS.append({"name": name, "args": {k: str(v)[:200] for k, v in args.items()}})
    return "отказ: пробный ход, инструменты не исполняются"


async def run() -> int:
    code_tree = _opt("--code", str(HERE))
    prompt_text = _opt("--prompt", PROBE_PROMPT)
    max_tokens = int(_opt("--max-tokens", "256"))
    iters = int(_opt("--iters", "1"))
    tmp = prepare(code_tree)
    try:
        from server import tools, rules, memory, pool, skills   # noqa: E402

        tree = Path(code_tree).expanduser().resolve()
        head = subprocess.run(["git", "-C", str(tree), "log", "--oneline", "-1"],
                              capture_output=True, text=True, timeout=30)
        agent_full = getattr(tools, "agent_system", None)
        agent_block = agent_full(allow_shell=True) if agent_full else tools.AGENT_SYSTEM
        rules_text = rules.compose()
        mcp_text = tools.mcp_block() or ""
        mem_text = memory.recall_block(prompt_text) or ""
        skills_text = skills.auto_block(prompt_text) or ""
        specs = tools.openai_specs()
        preflight = tools.preflight_block(specs, allow_shell=True)

        system = agent_block
        for part in (rules_text, skills_text, mcp_text, mem_text):
            if part:
                system = f"{system}\n\n{part}"
        prompt = f"{prompt_text}\n\n{preflight}"

        tools_chars = len(json.dumps(specs, ensure_ascii=False))
        print("=" * 78)
        print("ЖИВАЯ ПРОБА ПРЕФИКСА. Дерево кода: %s" % tree)
        print("Коммит: %s" % (head.stdout.strip() or "?"))
        print("Промт: %r   max_tokens=%d iters=%d" % (prompt_text[:70], max_tokens, iters))
        print("=" * 78)
        pieces = (("agent_system", len(agent_block)), ("rules", len(rules_text)),
                  ("skills(1С)", len(skills_text)), ("mcp_block", len(mcp_text)),
                  ("memory", len(mem_text)), ("preflight", len(preflight)),
                  ("tools JSON (%d)" % len(specs), tools_chars))
        for name, chars in pieces:
            print("  %-20s %7d зн" % (name, chars))
        print("  %-20s %7d зн" % ("ИТОГО префикс",
                                   len(system) + len(preflight) + tools_chars))

        res = await pool.ask("chat", prompt, system=system, role="chat",
                             max_tokens=max_tokens, history=[], turn_kind="inline",
                             tools=specs, tool_executor=_record, max_tool_iters=iters)
        usage = res.get("usage") or {}
        print()
        print("  ok=%s model=%s tier=%s escalated=%s"
              % (res.get("ok"), res.get("model"), res.get("tier"), res.get("escalated")))
        print("  trace=%s" % json.dumps(
            [{"model": t.get("model"), "ok": t.get("ok"), "err": (t.get("err") or "")[:60]}
             for t in (res.get("trace") or [])], ensure_ascii=False))
        print("  text=%r" % ((res.get("text") or "")[:120]))
        print("  PROMPT_TOKENS = %s   completion = %s   cache_read = %s"
              % (usage.get("prompt_tokens"), usage.get("completion_tokens"),
                 usage.get("cache_read_tokens")))
        print("  ВЫЗОВЫ ИНСТРУМЕНТОВ (%d): %s"
              % (len(CALLS), json.dumps(CALLS, ensure_ascii=False)[:600]))
        if not res.get("ok"):
            print("  err=%s" % (res.get("err") or "")[:300])
            return 1
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print("копия конфига и БД удалены: %s" % tmp)


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
