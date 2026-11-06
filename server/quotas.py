"""quotas.py — пробы квот каналов (read-only).

Дисциплина данных: неполные данные → remaining=None, НЕ оценка. Каждая проба
пишется в quota_readings (сырой ответ усечён до 2КБ). У Token Plan Alibaba
публичного usage-эндпоинта нет — burn считается по telemetry, живость
канала проверяется pool'ом (не здесь).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import accounts
from .config import ChannelCfg, load_config
from .db import get_db, now_iso

KIMI_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
_RAW_LIMIT = 2048


def probe_all() -> list[dict]:
    """Пробует все enabled-каналы, каждую пробу пишет в quota_readings."""
    out = []
    for ch in load_config().channels.values():
        if not ch.enabled:
            continue
        if ch.kind == "kimi":
            r = probe_kimi(ch)
        elif ch.kind == "alibaba":
            r = probe_alibaba(ch)
        elif ch.kind == "ollama":
            r = probe_ollama(ch)
        elif ch.kind == "zen":
            r = probe_zen(ch)
        else:
            r = {"ok": False, "remaining": None, "unit": None, "window": None,
                 "raw": {"note": f"unknown channel kind: {ch.kind}"}}
        out.append(_store(ch.name, r))
        for extra in r.get("_extra", []):
            out.append(_store(ch.name, extra))
    return out


def _store(channel: str, r: dict) -> dict:
    raw = r.get("raw")
    raw_s = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
    rec = {"ts": now_iso(), "channel": channel, "ok": bool(r.get("ok")),
           "remaining": r.get("remaining"), "unit": r.get("unit"),
           "window": r.get("window"), "raw": raw_s[:_RAW_LIMIT]}
    db = get_db()
    db.execute(
        "INSERT INTO quota_readings (ts, channel, ok, remaining, unit, window, raw)"
        " VALUES (?,?,?,?,?,?,?)",
        (rec["ts"], channel, int(rec["ok"]), rec["remaining"], rec["unit"],
         rec["window"], rec["raw"]))
    db.commit()
    return rec


def probe_kimi(ch: ChannelCfg) -> dict:
    """kimi-cli: OAuth-токен живёт ~15 минут, перед пробой обновляем
    (accounts._refresh_kimi), иначе каждая проба говорила бы «протух»."""
    cred_path = Path(accounts.credentials_file(ch)).expanduser()
    try:
        cred = json.loads(cred_path.read_text(encoding="utf-8"))
    except Exception as e:
        return _bad(f"credentials unreadable: {e}")
    exp = _parse_expiry(cred.get("expires_at"))
    if exp is not None and exp < datetime.now(timezone.utc):
        cred["access_token"] = accounts._refresh_kimi(cred_path, cred)
        exp = _parse_expiry(cred.get("expires_at"))
        if exp is not None and exp < datetime.now(timezone.utc):
            return _bad("oauth token expired и refresh не удался — гаснущий резерв")
    try:
        r = httpx.get(KIMI_USAGE_URL, timeout=20,
                      headers={"Authorization": f"Bearer {cred.get('access_token', '')}"})
    except Exception as e:
        return _bad(f"request failed: {e}")
    if r.status_code != 200:
        return {"ok": False, "remaining": None, "unit": None, "window": None,
                "raw": {"http": r.status_code, "body": r.text[:1000]}}
    try:
        data = r.json()
    except Exception:
        data = {"body": r.text[:1000]}
    remaining, unit, window = _extract_remaining(data, ch)
    rec = {"ok": True, "remaining": remaining, "unit": unit, "window": window, "raw": data}
    # BioClaude-паттерн «два окна»: 5ч и 7д храним как отдельные пробы.
    usages = data.get("usages") if isinstance(data, dict) else None
    if isinstance(usages, dict):
        for wname, wlabel in (("limit_5h", "5h"), ("limit_7d", "7d")):
            w = usages.get(wname)
            if wlabel != window and isinstance(w, dict) and isinstance(w.get("used_ratio"), (int, float)):
                rec.setdefault("_extra", []).append(
                    {"ok": True, "remaining": round((1 - float(w["used_ratio"])) * 100, 1),
                     "unit": "percent", "window": wlabel,
                     "raw": {"reset_time": w.get("reset_time")}})
    # Абсолютные счётчики вызовов (usage + limits[]) — иначе картина «99.9%»
    # по узкому окну маскирует израсходованную недельную пачку (quota_unit).
    det = data.get("usage") if isinstance(data, dict) else None
    if isinstance(det, dict):
        try:
            rec.setdefault("_extra", []).append(
                {"ok": True, "remaining": int(float(det["remaining"])),
                 "unit": ch.quota_unit or "calls", "window": "7d-calls",
                 "raw": {"limit": det.get("limit"), "used": det.get("used"),
                         "reset_time": det.get("resetTime")}})
        except (KeyError, TypeError, ValueError):
            pass
    for lim in data.get("limits") or [] if isinstance(data, dict) else []:
        try:
            w = lim["window"]; d = lim["detail"]
            label = f"{w['duration']}{str(w.get('timeUnit','')).replace('TIME_UNIT_','').lower()}"
            rec.setdefault("_extra", []).append(
                {"ok": True, "remaining": int(float(d["remaining"])),
                 "unit": ch.quota_unit or "calls", "window": label,
                 "raw": {"limit": d.get("limit"), "used": d.get("used"),
                         "reset_time": d.get("resetTime")}})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return rec


def probe_alibaba(ch: ChannelCfg) -> dict:
    # Публичного usage-эндпоинта у Token Plan нет — URL не изобретаем.
    return {"ok": False, "remaining": None, "unit": None, "window": None,
            "raw": {"note": "no usage endpoint; burn считается по telemetry"}}


def probe_zen(ch: ChannelCfg) -> dict:
    """Zen (opencode Go API): usage-эндпоинта нет — проба живости через
    GET /models (обязателен заголовок x-opencode-session, см. pool.py).
    Квота неизвестна → remaining=None (честно, не оценка)."""
    try:
        r = httpx.get(ch.base_url.rstrip("/") + "/models", timeout=15,
                      headers={"Authorization": f"Bearer {accounts.api_key(ch)}",
                               "x-opencode-session": "quota-probe"})
    except Exception as e:
        return _bad(f"unreachable: {e}")
    if r.status_code != 200:
        return {"ok": False, "remaining": None, "unit": None, "window": None,
                "raw": {"http": r.status_code, "body": r.text[:1000]}}
    try:
        models = [m.get("id") for m in r.json().get("data") or []]
    except Exception:
        models = []
    return {"ok": True, "remaining": None, "unit": None, "window": None,
            "raw": {"models": models[:50]}}


def probe_ollama(ch: ChannelCfg) -> dict:
    """Живость локального тира + мониторинг простоя (загруженные модели)."""
    base = ch.base_url.rstrip("/")
    try:
        r = httpx.get(f"{base}/api/tags", timeout=3)
        if r.status_code != 200:
            return {"ok": False, "remaining": None, "unit": None, "window": None,
                    "raw": {"http": r.status_code}}
    except Exception as e:
        return _bad(f"unreachable: {e}")
    raw: dict = {}
    try:
        ps = httpx.get(f"{base}/api/ps", timeout=3)
        if ps.status_code == 200:
            models = ps.json().get("models") or []
            raw["loaded_models"] = [m.get("name") or m.get("model") for m in models]
    except Exception as e:
        raw["ps_error"] = str(e)
    return {"ok": True, "remaining": None, "unit": None, "window": None, "raw": raw}


def latest() -> list[dict]:
    """Последняя проба на (канал, окно) — Kimi отдаёт 5h и 7d раздельно."""
    rows = get_db().execute(
        """SELECT channel, ok, remaining, unit, window, ts FROM quota_readings q
           WHERE id = (SELECT MAX(id) FROM quota_readings
                       WHERE channel = q.channel AND window IS q.window)
           ORDER BY channel, window""").fetchall()
    return [{"channel": r["channel"], "ok": bool(r["ok"]), "remaining": r["remaining"],
             "unit": r["unit"], "window": r["window"], "ts": r["ts"]} for r in rows]


def _bad(note: str) -> dict:
    return {"ok": False, "remaining": None, "unit": None, "window": None,
            "raw": {"note": note}}


def _parse_expiry(v) -> datetime | None:
    """expires_at из credentials: epoch (с/мс) или ISO; непонятное → None."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, tz=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _extract_remaining(data, ch: ChannelCfg):
    """Разбор ответа usages. Сначала известная структура Kimi
    (usages.limit_5h.used_ratio — самое узкое окно), потом generic-fallback.
    Ничего не выдумываем: не нашли → None."""
    if isinstance(data, dict):
        usages = data.get("usages")
        if isinstance(usages, dict):
            for wname, wlabel in (("limit_5h", "5h"), ("limit_7d", "7d")):
                w = usages.get(wname)
                if isinstance(w, dict) and isinstance(w.get("used_ratio"), (int, float)):
                    return round((1 - float(w["used_ratio"])) * 100, 1), "percent", wlabel
        det = data.get("usage")
        if isinstance(det, dict):
            try:
                lim, rem = float(det["limit"]), float(det["remaining"])
                if lim > 0:
                    return round(rem / lim * 100, 1), "percent", None
            except (KeyError, TypeError, ValueError):
                pass

    # fallback: точные имена ключей, НЕ подстроки («duration» содержит «ratio»!)
    remaining, unit, window = None, None, None
    EXACT_PCT = {"percent", "remaining_percent", "used_ratio", "ratio"}
    EXACT_REM = {"remaining", "left", "balance"}

    def walk(node):
        nonlocal remaining, unit, window
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                if remaining is None and isinstance(v, (int, float)) and not isinstance(v, bool):
                    if kl in EXACT_PCT:
                        remaining, unit = float(v), "percent"
                    elif kl in EXACT_REM:
                        remaining, unit = float(v), ch.quota_unit
                if window is None and isinstance(v, str) and kl == "window":
                    window = v
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return remaining, unit, window
