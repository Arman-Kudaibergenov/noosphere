"""accounts.py — пул аккаунтов канала (FOUNDING §9 «Пул аккаунтов», батч 4).

Конфиг канала, опционально:
    channels:
      alibaba:
        accounts:
          - {label: основной, api_key_file: ~/.secrets/alibaba-token-plan.key}
          - {label: запасной, api_key_env: ALIBABA_SPARE_KEY}

Правила (из спецификации bioclaude, адаптировано под нашу экономику):
- ЛИПКОСТЬ: текущий аккаунт хранится в meta (pool:account:<channel>) и не меняется,
  пока работает. «Рядом свободнее» — НЕ повод для переезда (переезд = потеря
  prompt-кэша), поэтому окнами квот v1 не скорит — ротация только по отказу.
- РОТАЦИЯ: при ошибке класса auth/rate-limit текущий аккаунт уходит на cooldown
  (10 мин, meta pool:cool:<channel>:<idx>), берётся следующий не охлаждённый.
  Аккаунт один → rotate() вернёт None (честно: ротировать некуда).
- WRAP-UP: завершённый ход никогда не повторяется на другом аккаунте — ротация
  происходит только внутри упавшего вызова, до финала хода (pool.ask).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ChannelCfg
from .db import get_db, now_iso

COOLDOWN_MINUTES = 10


def _meta_get(key: str) -> str | None:
    row = get_db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(key: str, value: str) -> None:
    db = get_db()
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))
    db.commit()


def accounts_of(ch: ChannelCfg) -> list[dict]:
    """Список аккаунтов канала. Нет секции accounts — один «неявный» аккаунт
    из полей самого канала (api_key_file/env, credentials_file)."""
    accs = getattr(ch, "accounts", None) or []
    if accs:
        return accs
    return [{"label": "default",
             "api_key_file": ch.api_key_file, "api_key_env": ch.api_key_env,
             "credentials_file": ch.credentials_file}]


def current_index(ch: ChannelCfg) -> int:
    try:
        idx = int(_meta_get(f"pool:account:{ch.name}") or 0)
    except ValueError:
        idx = 0
    return idx if 0 <= idx < len(accounts_of(ch)) else 0


def _on_cooldown(ch: ChannelCfg, idx: int) -> bool:
    ts = _meta_get(f"pool:cool:{ch.name}:{idx}")
    if not ts:
        return False
    try:
        until = datetime.fromisoformat(ts)
    except ValueError:
        return False
    return datetime.now(timezone.utc) < until


KIMI_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"  # публичный client_id kimi CLI
KIMI_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"  # живая проба 2026-10-13


def _refresh_kimi(p: Path, cred: dict, proxy: str = "") -> str:
    """OAuth-токен kimi живёт ~15 минут (expires_in=900): при истечении —
    refresh по refresh_token и перезапись credentials-файла (как делает сам
    kimi CLI). Провал refresh — возвращаем что было: вызов упадёт с auth и
    уйдёт в телеметрию, а не в молчаливую подмену."""
    exp = cred.get("expires_at")
    rt = cred.get("refresh_token")
    try:
        fresh = exp is None or float(exp) > datetime.now(timezone.utc).timestamp() + 60
    except (TypeError, ValueError):
        fresh = True  # непонятный формат — не мешаем, пусть транспорт решает
    if fresh or not rt:
        return cred.get("access_token", "")
    try:
        import httpx
        # 2026-11-04: прокси может быть мёртв (vps nl лёг) — refresh, как и
        # обычный вызов (pool._post), получает запасной прямой маршрут
        try:
            r = httpx.post(KIMI_TOKEN_URL,
                           data={"client_id": KIMI_CLIENT_ID, "grant_type": "refresh_token",
                                 "refresh_token": rt},
                           headers={"User-Agent": "kimi-code/0.39"}, timeout=30,
                           proxy=proxy or None)
        except Exception:
            if not proxy:
                raise
            print(f"[accounts] kimi refresh: маршрут {proxy} отказал — напрямую",
                  file=sys.stderr)
            r = httpx.post(KIMI_TOKEN_URL,
                           data={"client_id": KIMI_CLIENT_ID, "grant_type": "refresh_token",
                                 "refresh_token": rt},
                           headers={"User-Agent": "kimi-code/0.39"}, timeout=30)
        if r.status_code != 200:
            print(f"[accounts] kimi refresh: http {r.status_code}", file=sys.stderr)
            return cred.get("access_token", "")
        resp = r.json()
        cred.update({
            "access_token": resp.get("access_token", cred.get("access_token", "")),
            "refresh_token": resp.get("refresh_token", rt),
            "token_type": resp.get("token_type", cred.get("token_type", "Bearer")),
            "expires_in": resp.get("expires_in", 900),
            "expires_at": int(datetime.now(timezone.utc).timestamp())
                          + int(resp.get("expires_in", 900)),
        })
        p.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[accounts] kimi refresh упал: {e!r}", file=sys.stderr)
    return cred.get("access_token", "")


def _key_of_account(acc: dict, proxy: str = "") -> str:
    if acc.get("api_key_env"):
        import os
        if os.environ.get(acc["api_key_env"]):
            return os.environ[acc["api_key_env"]]
    if acc.get("api_key_file"):
        p = Path(acc["api_key_file"]).expanduser()
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    if acc.get("credentials_file"):  # OAuth-формат kimi-cli
        p = Path(acc["credentials_file"]).expanduser()
        if p.exists():
            try:
                return _refresh_kimi(p, json.loads(p.read_text(encoding="utf-8")), proxy)
            except json.JSONDecodeError:
                return ""
    return ""


def api_key(ch: ChannelCfg) -> str:
    """Ключ текущего аккаунта канала (заменяет ch.api_key() в pool/probe)."""
    return _key_of_account(accounts_of(ch)[current_index(ch)], getattr(ch, "proxy", ""))


def credentials_file(ch: ChannelCfg) -> str:
    """credentials_file текущего аккаунта (проба квот kimi)."""
    return accounts_of(ch)[current_index(ch)].get("credentials_file") or ch.credentials_file


def rotate(ch: ChannelCfg) -> int | None:
    """Текущий аккаунт — на cooldown, переключаемся на следующий свободный.
    Возвращает новый индекс или None (все на cooldown / аккаунт один)."""
    accs = accounts_of(ch)
    if len(accs) < 2:
        return None
    cur = current_index(ch)
    until = (datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)).isoformat()
    _meta_set(f"pool:cool:{ch.name}:{cur}", until)
    for step in range(1, len(accs)):
        nxt = (cur + step) % len(accs)
        if not _on_cooldown(ch, nxt):
            _meta_set(f"pool:account:{ch.name}", str(nxt))
            return nxt
    return None
