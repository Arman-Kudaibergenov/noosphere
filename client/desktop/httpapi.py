"""HTTP-слой оболочки: проба /health и обычные запросы к API сервера.

Всё на urllib (stdlib). Ошибки не бросаются наружу, а возвращаются в `Reply.error`
— наблюдатель и диагностика обязаны работать при мёртвом сервере и молча не
штормить (урок аудита r4: transport_broken штормил).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import appconfig


@dataclass
class Reply:
    ok: bool
    status: int = 0
    data: Any = None
    error: str = ""
    ms: int = 0


@dataclass
class Probe:
    url: str
    label: str
    ok: bool = False
    status: int = 0
    version: str = ""
    error: str = ""
    ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


def label_for(url: str) -> str:
    if "192.168." in url:
        return "LAN"
    if url.startswith("http://100.") or url.startswith("https://100."):
        return "tailnet"
    if "127.0.0.1" in url or "localhost" in url:
        return "local"
    return "host"


def request(server: str, path: str, token: str = "", method: str = "GET",
            body: dict | None = None, timeout: float = appconfig.API_TIMEOUT) -> Reply:
    """Один запрос к серверу. `server` без завершающего слэша."""
    url = server.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            ms = int((time.perf_counter() - started) * 1000)
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return Reply(False, resp.status, None, "ответ не JSON", ms)
            return Reply(True, resp.status, payload, "", ms)
    except urllib.error.HTTPError as exc:
        ms = int((time.perf_counter() - started) * 1000)
        note = "токен отклонён" if exc.code in (401, 403) else f"HTTP {exc.code}"
        return Reply(False, exc.code, None, note, ms)
    except urllib.error.URLError as exc:
        ms = int((time.perf_counter() - started) * 1000)
        return Reply(False, 0, None, f"сеть: {getattr(exc, 'reason', exc)}", ms)
    except TimeoutError:
        ms = int((time.perf_counter() - started) * 1000)
        return Reply(False, 0, None, "таймаут", ms)
    except OSError as exc:
        ms = int((time.perf_counter() - started) * 1000)
        return Reply(False, 0, None, f"{type(exc).__name__}: {exc}", ms)


def probe(server: str, timeout: float = appconfig.PROBE_TIMEOUT) -> Probe:
    """Проба /health: жив ли сервер и какая у него версия."""
    out = Probe(url=server.rstrip("/"), label=label_for(server))
    started = time.perf_counter()
    reply = request(out.url, "/health", timeout=timeout)
    out.ms = int((time.perf_counter() - started) * 1000)
    out.status = reply.status
    if reply.ok and isinstance(reply.data, dict):
        out.ok = reply.data.get("status") == "ok"
        out.version = str(reply.data.get("version", ""))
        out.error = "" if out.ok else f"health вернул status={reply.data.get('status')!r}"
    else:
        out.error = reply.error or "нет ответа"
    return out


def probe_all(servers: tuple[str, ...] | None = None,
              timeout: float = appconfig.PROBE_TIMEOUT) -> list[Probe]:
    targets = servers if servers is not None else appconfig.servers()
    return [probe(url, timeout) for url in targets]


def pick_server(probes: list[Probe]) -> Probe | None:
    for item in probes:
        if item.ok:
            return item
    return None
