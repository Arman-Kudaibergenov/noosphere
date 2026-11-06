#!/usr/bin/env python3
"""Проба каналов пула подписок (фаза 5, 2026-11-05).

Панель «Подписки» (panel.py) показывает cooldown'ы ротации — но канал, который
давно не вызывался, выглядит «живым» даже при мёртвой квоте (прецедент: alibaba
висел «жива» при квоте, исчерпанной до 12.11). Этот скрипт дешёво зондирует
каждый канал и пишет результат в meta pool:probe:<channel> — панель читает его.

Стоимость: 401/429 бесплатны; успешная 1-токеновая проба стоит копейки.
kimi не зондируется активно: канал под постоянной боевой нагрузкой, состояние
видно по cooldown'ам ротации.

Запуск: cd ~/noosphere && .venv/bin/python scripts/subscriptions_probe.py
Cron: */30 * * * *
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import load_config          # noqa: E402
from server import accounts as acc             # noqa: E402

TIMEOUT = 15

# Модель для 1-токеновой пробы: у канала в config.yaml её нет (модель задаёт
# роль/tier), а gateway станции на чужое имя отвечает 401 (проверено 05.11).
PROBE_MODEL = {"station": "local/deepseek-flash"}


def _record(channel: str, ok: bool, err: str = "", reset: str = "") -> None:
    acc._meta_set(f"pool:probe:{channel}", json.dumps(
        {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "ok": ok, "err": err[:200], "reset": reset}, ensure_ascii=False))


def _reset_of(body: str) -> str:
    """Из 429 Alibaba «quota will reset at 11-12 16:00:00 UTC» → «12.11 16:00 UTC»."""
    m = re.search(r"reset at (\d{2})-(\d{2}) (\d{2}:\d{2})", body)
    return f"{m.group(2)}.{m.group(1)} {m.group(3)} UTC" if m else ""


def _probe_openai(base: str, key: str, model: str) -> tuple[bool, str, str]:
    """1-токеновый chat completion. Возвращает (ok, err, reset)."""
    body = json.dumps({"model": model, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "ping"}]}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            json.loads(r.read().decode("utf-8", "replace"))
        return True, "", ""
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        return False, f"HTTP {e.code}", _reset_of(detail)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", ""


def _probe_models(base: str, key: str = "") -> tuple[bool, str, str]:
    """Дешёвый GET /models (ollama: /api/tags)."""
    url = base.rstrip("/")
    url += "/api/tags" if "11434" in url else "/models"
    headers = {"Authorization": "Bearer " + key} if key else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return (r.status == 200), "", ""
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", ""


def main() -> int:
    cfg = load_config()
    for ch in cfg.channels.values():
        if ch.kind == "kimi":
            continue  # под боевой нагрузкой — активная проба не нужна
        try:
            accs = acc.accounts_of(ch)
            key = acc._key_of_account(accs[acc.current_index(ch)]) if accs else ""
            if ch.kind == "ollama":
                ok, err, reset = _probe_models(ch.base_url)
            elif ch.base_url and key:
                model = PROBE_MODEL.get(ch.name) or "deepseek-v4.1-flash"
                ok, err, reset = _probe_openai(ch.base_url, key, model)
            elif ch.base_url:
                ok, err, reset = _probe_models(ch.base_url, key)
            else:
                _record(ch.name, False, "нет base_url")
                continue
            _record(ch.name, ok, err, reset)
            print(f"{ch.name}: {'ok' if ok else 'FAIL ' + err}"
                  + (f" (ресет {reset})" if reset else ""))
        except Exception as e:  # noqa: BLE001 — один канал не роняет пробы остальных
            _record(ch.name, False, f"probe error: {type(e).__name__}: {e}"[:200])
            print(f"{ch.name}: probe error {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
