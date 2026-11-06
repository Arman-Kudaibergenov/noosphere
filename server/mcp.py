"""mcp.py — минимальный MCP streamable-HTTP клиент (1С-контур, 2026-10-13).

Конфиг mcp.servers.<name>: {url, desc, enabled, headers}. Поток на каждый вызов:
initialize (protocolVersion, читаем Mcp-Session-Id из заголовков ответа) →
notifications/initialized → tools/list | tools/call. Ответ может быть JSON
или SSE (text/event-stream) — парсим оба. Сессия на вызов открывается заново:
простота важнее экономии (серверы в LAN, рукопожатие ~10 мс).

Живые пробы 2026-10-13: testpilot-farm http://192.168.0.31:6004/mcp,
bsl-atlas ТМС http://192.168.3.136:8014/mcp (путь / отвечает 404).
"""
from __future__ import annotations

import json
import os
import time

import httpx

from .config import load_config

CALL_CAP = 8_000         # символов результата tools/call для модели
DESC_CAP = 300           # символов описания инструмента в tools/list
_CACHE_TTL = 600         # сек, кэш tools/list на сервер
_TOOLS_CACHE: dict[str, tuple[float, list[dict]]] = {}

PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "noosphere", "version": "1.7"}

_HEADERS = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}


def parse_sse(body: str) -> list[str]:
    """Содержимое data:-строк SSE-потока (многострочный data склеивается \\n)."""
    out: list[str] = []
    data_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line.strip():
            if data_lines:
                out.append("\n".join(data_lines))
                data_lines = []
    if data_lines:
        out.append("\n".join(data_lines))
    return out


def _parse_response(r: httpx.Response, want_id: int) -> tuple[str | None, dict]:
    """(session_id, result|error) из JSON или SSE-ответа."""
    sid = r.headers.get("mcp-session-id")
    ctype = r.headers.get("content-type", "")
    payloads: list[str]
    if "text/event-stream" in ctype:
        payloads = parse_sse(r.text)
    else:
        payloads = [r.text]
    for p in payloads:
        try:
            msg = json.loads(p)
        except ValueError:
            continue
        if msg.get("id") == want_id:
            if "error" in msg:
                return sid, {"error": msg["error"]}
            return sid, {"result": msg.get("result")}
    return sid, {"error": f"нет ответа с id={want_id} (http {r.status_code})"}


def _server_headers(srv: dict) -> dict:
    """Заголовки сервера из конфига — нужны MCP с авторизацией (напр. farm-router).

    Значение — строка или {from_file: путь}: секрет держим в файле, а не в конфиге,
    по образцу channels.<name>.api_key_file. Нечитаемый файл молча пропускаем."""
    out: dict = {}
    for key, val in (srv.get("headers") or {}).items():
        if isinstance(val, dict) and val.get("from_file"):
            try:
                with open(os.path.expanduser(str(val["from_file"])), encoding="utf-8") as fh:
                    val = fh.read().strip()
            except OSError:
                continue
        out[str(key)] = str(val)
    return out


async def _post(url: str, payload: dict, session_id: str | None,
                want_id: int, timeout: float = 30,
                extra: dict | None = None) -> tuple[str | None, dict]:
    headers = {**_HEADERS, **(extra or {})}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload, headers=headers)
    return _parse_response(r, want_id)


async def _open_session(url: str, extra: dict | None = None) -> str:
    sid, res = await _post(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": _CLIENT_INFO}}, None, 1, extra=extra)
    if "error" in res or not sid:
        raise RuntimeError(f"initialize не удался: {res.get('error') or 'нет Mcp-Session-Id'}")
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(url, json={"jsonrpc": "2.0",
                                     "method": "notifications/initialized"},
                          headers={**_HEADERS, **(extra or {}), "Mcp-Session-Id": sid})
    return sid


def _server_cfg(name: str) -> dict:
    srv = (load_config().mcp.servers or {}).get(name)
    if not srv:
        raise KeyError(f"MCP-сервер {name!r} не настроен (mcp.servers)")
    if not srv.get("enabled"):
        raise RuntimeError(f"MCP-сервер {name!r} выключен конфигом")
    if not srv.get("url"):
        raise RuntimeError(f"MCP-сервер {name!r}: пустой url")
    return srv


async def tools_list(name: str, *, force: bool = False) -> list[dict]:
    """Список инструментов сервера (кэш TTL 10 мин, описания ≤300 символов)."""
    hit = _TOOLS_CACHE.get(name)
    if hit and not force and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    srv = _server_cfg(name)
    extra = _server_headers(srv)
    sid = await _open_session(srv["url"], extra)
    _, res = await _post(srv["url"], {"jsonrpc": "2.0", "id": 2,
                                      "method": "tools/list"}, sid, 2, extra=extra)
    if "error" in res:
        raise RuntimeError(f"tools/list: {res['error']}")
    tools = [{"name": t.get("name", "?"),
              "description": (t.get("description") or "")[:DESC_CAP]}
             for t in (res["result"] or {}).get("tools") or []]
    _TOOLS_CACHE[name] = (time.time(), tools)
    return tools


async def call_tool(name: str, tool: str, arguments: dict) -> str:
    """Вызов инструмента; текстовые content-блоки склеиваются, cap 8К."""
    srv = _server_cfg(name)
    extra = _server_headers(srv)
    sid = await _open_session(srv["url"], extra)
    _, res = await _post(srv["url"], {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments}}, sid, 3, timeout=60, extra=extra)
    if "error" in res:
        raise RuntimeError(f"tools/call {tool}: {res['error']}")
    result = res["result"] or {}
    parts: list[str] = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    text = "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)
    if result.get("isError"):
        text = "[MCP isError] " + text
    if len(text) > CALL_CAP:
        text = text[:CALL_CAP] + f"\n[…обрезано, было {len(text)} символов]"
    return text


def servers_status() -> list[dict]:
    """Снимок конфигурации для диагностики (без сетевых проб)."""
    return [{"name": n, "url": s.get("url", ""), "desc": s.get("desc", ""),
             "enabled": bool(s.get("enabled", True)),
             "cached_tools": len(_TOOLS_CACHE.get(n, (0, []))[1])}
            for n, s in (load_config().mcp.servers or {}).items()]
