#!/usr/bin/env python3
"""measure_prefix.py — замер статического префикса хода по контекстам.

Считает то, что уходит в модель ДО текста задачи: системный блок (AGENT_SYSTEM
или EXEC_SYSTEM + правила рабочего места + список MCP + память разговоров +
каталог скилов), префлайт в промпте и JSON-схемы инструментов. Правила и память
берутся из КОПИИ боевой БД, поэтому цифры совпадают с боем; копия удаляется.

Контексты (те же, что собирают main._run_turn_inner и devexec):
  chat_tools    — интерактивный ход владельца с инструментами и shell;
  bg_no_shell   — фоновый прогон (kind=run без allow_shell): shell выключен;
  devexec       — исполнитель прогонов доработки (EXEC_SYSTEM, свой состав тулов);
  skills_1c     — chat_tools + каталог 1С-скилов (липкий на сессию).

Прогон: ~/noosphere/.venv/bin/python scripts/measure_prefix.py [--keep]
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = Path(os.path.expanduser("~/noosphere"))
PROBE_PROMPT = "Проба размера префикса. Ответь одним словом: ОК"
ONE_C_PROMPT = "Нужен отчет по 1С УТ 11: взаиморасчеты с контрагентами, СКД"
KEEP = "--keep" in sys.argv


def _prepare() -> str:
    """Копия боевого конфига и БД во временный каталог; возвращает data_dir."""
    tmp = tempfile.mkdtemp(prefix="ns-prefix-")
    src = (LIVE / "config.yaml").read_text(encoding="utf-8")
    if re.search(r"(?m)^data_dir:.*$", src):
        src = re.sub(r"(?m)^data_dir:.*$", "data_dir: %s" % json.dumps(tmp), src)
    else:
        src += "\ndata_dir: %s\n" % json.dumps(tmp)
    (Path(tmp) / "config.yaml").write_text(src, encoding="utf-8")
    os.environ["NOOSPHERE_CONFIG"] = str(Path(tmp) / "config.yaml")
    # снимок боевой БД через backup API: согласованный, WAL included, источник read-only
    src_db = sqlite3.connect(f"file:{LIVE / 'data' / 'noosphere.db'}?mode=ro", uri=True)
    dst_db = sqlite3.connect(str(Path(tmp) / "noosphere.db"))
    with dst_db:
        src_db.backup(dst_db)
    src_db.close()
    dst_db.close()
    return tmp


def _ctx(name: str, pieces: dict, tools_json: int) -> dict:
    sys_chars = sum(pieces.values())
    total = sys_chars + tools_json
    return {"context": name, "pieces": pieces, "system_chars": sys_chars,
            "tools_json_chars": tools_json, "total_chars": total,
            "est_tokens": round(total / 3.0)}


def main() -> int:
    tmp = _prepare()
    try:
        sys.path.insert(0, str(ROOT))
        from server import tools, pool, skills, rules, memory          # noqa: E402
        try:
            from server import devexec
        except Exception as e:                                          # noqa: BLE001
            devexec = None
            print("devexec не импортирован: %r" % (e,))

        agent_full = getattr(tools, "agent_system", None)
        shell_block = agent_full(allow_shell=True) if agent_full else tools.AGENT_SYSTEM
        noshell_block = agent_full(allow_shell=False) if agent_full else tools.AGENT_SYSTEM
        devrun_block = agent_full(allow_shell=False, devrun=False) if agent_full else ""

        all_specs = tools.openai_specs()
        no_shell_specs = [s for s in all_specs if s["function"]["name"]
                          not in ("shell_exec", "terminal_exec")]
        rules_text = rules.compose()
        mcp_text = tools.mcp_block() or ""
        mem_text = memory.recall_block(PROBE_PROMPT) or ""
        skills_1c = skills.auto_block(ONE_C_PROMPT) or ""
        skills_any = skills.auto_block() or ""

        print("=" * 84)
        print("ЗАМЕР СТАТИЧЕСКОГО ПРЕФИКСА (знаки). Дерево: %s" % ROOT)
        head = subprocess.run(["git", "-C", str(ROOT), "log", "--oneline", "-1"],
                              capture_output=True, text=True, timeout=30)
        print("Код: %s" % (head.stdout.strip() or "?"))
        print("=" * 84)
        print("Промт пробы: %r" % PROBE_PROMPT)
        print()
        print("--- куски, общие для контекстов")
        common = [
            ("agent_system(shell)", len(shell_block)),
            ("agent_system(no shell)", len(noshell_block)),
            ("agent_system(без devrun, no shell)", len(devrun_block)),
            ("rules.compose()  [правит владелец]", len(rules_text)),
            ("mcp_block (список серверов)", len(mcp_text)),
            ("memory.recall_block (проба)", len(mem_text)),
            ("skills.auto_block (1С-тема)", len(skills_1c)),
            ("skills.auto_block (без фильтра)", len(skills_any)),
            ("preflight_block(shell)", len(tools.preflight_block(all_specs, allow_shell=True))),
            ("preflight_block(no shell)", len(tools.preflight_block(no_shell_specs, allow_shell=False))),
            ("NO_TOOLS_NOTE (ход без тулов)", len(pool.NO_TOOLS_NOTE)),
            ("tools JSON: %d инструментов" % len(all_specs), len(json.dumps(all_specs, ensure_ascii=False))),
            ("tools JSON без shell: %d" % len(no_shell_specs), len(json.dumps(no_shell_specs, ensure_ascii=False))),
        ]
        if devexec is not None:
            ex_specs = devexec._specs()
            common += [("EXEC_SYSTEM (исполнитель прогонов)", len(devexec.EXEC_SYSTEM)),
                       ("devexec tools JSON: %d" % len(ex_specs),
                        len(json.dumps(ex_specs, ensure_ascii=False)))]
        for name, chars in common:
            print("  %-42s %7d зн  ~%5d tok" % (name, chars, round(chars / 3.0)))

        print()
        print("--- инструменты поштучно (JSON-схема)")
        per = sorted(((s["function"]["name"], len(json.dumps(s, ensure_ascii=False)),
                       len(str(s["function"].get("description") or "")))
                      for s in all_specs), key=lambda r: -r[1])
        for name, chars, descr in per:
            print("  %-18s %6d зн   (описание %5d зн)" % (name, chars, descr))
        print("  %-18s %6d зн   (описания %5d зн)"
              % ("ИТОГО", sum(r[1] for r in per), sum(r[2] for r in per)))

        ctxs = []
        ctxs.append(_ctx("chat_tools", {
            "agent_system": len(shell_block), "rules": len(rules_text),
            "mcp_block": len(mcp_text), "memory": len(mem_text),
            "preflight": len(tools.preflight_block(all_specs, allow_shell=True)),
        }, len(json.dumps(all_specs, ensure_ascii=False))))
        ctxs.append(_ctx("bg_no_shell", {
            "agent_system": len(noshell_block), "rules": len(rules_text),
            "mcp_block": len(mcp_text), "memory": len(mem_text),
            "preflight": len(tools.preflight_block(no_shell_specs, allow_shell=False)),
        }, len(json.dumps(no_shell_specs, ensure_ascii=False))))
        if devexec is not None:
            ctxs.append(_ctx("devexec", {"EXEC_SYSTEM": len(devexec.EXEC_SYSTEM)},
                             len(json.dumps(devexec._specs(), ensure_ascii=False))))
        ctxs.append(_ctx("skills_1c", {
            "agent_system": len(shell_block), "rules": len(rules_text),
            "mcp_block": len(mcp_text), "memory": len(mem_text),
            "skills": len(skills_1c),
            "preflight": len(tools.preflight_block(all_specs, allow_shell=True)),
        }, len(json.dumps(all_specs, ensure_ascii=False))))

        print()
        print("--- ИТОГ ПО КОНТЕКСТАМ (system + preflight + tools JSON; без текста задачи)")
        print("  %-14s %10s %10s %10s %10s" % ("контекст", "system", "tools", "всего зн", "~токенов"))
        for c in ctxs:
            print("  %-14s %10d %10d %10d %10d"
                  % (c["context"], c["system_chars"], c["tools_json_chars"],
                     c["total_chars"], c["est_tokens"]))
        print()
        print("  справка: живой замер пустого чат-хода 02.11 (дерево 253ec19) — "
              "4951 prompt-токенов при chat_tools; rules+memory — данные, не код.")
        if skills_1c:
            lines = [ln for ln in skills_1c.splitlines() if ln.startswith("- ")]
            print("  каталог скилов: строк %d, средняя длина строки %d зн"
                  % (len(lines), sum(len(x) for x in lines) // max(1, len(lines))))
        return 0
    finally:
        if KEEP:
            print("каталог замера оставлен: %s" % tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
            print("копия конфига и БД удалены: %s" % tmp)


if __name__ == "__main__":
    sys.exit(main())
