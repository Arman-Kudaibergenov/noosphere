#!/usr/bin/env python3
"""smoke.py — дымовая проверка Noosphere без сети.

Прогон: временный data_dir, transport=log, stt_url — мёртвая заглушка
(127.0.0.1:1), каналы выключены/мёртвы:
1. health; 2. /v1/chat на выключенных каналах → ok:false «нет доступного тира»,
   но turn и telemetry записаны; 3. sessions create/get/resume (+ state/queue_len);
4. quota/burn/alerts эндпоинты отвечают; 5. очередь: BUSY → queued:true,
   steer-приоритет, cancel; 6. FTS-поиск по prompt; 7. вложения чанками;
8. расписания: create/toggle/delete; 8b-h: tools/terminal/runs/rules/memory/
   vault/джейл/deny-list/папки/stt-контракт/route/проекты;
8i-s (аудит r3): WS /v1/ws, alerts ack + статусы + backoff недоставленного crit,
   vault REST + at-rest шифрование + маскировка reveal, acceptance (run-ходы не
   засчитываются), quota/probe офлайн, list-эндпоинты с параметрами, негативы
   батча 6 + stt 400/413 + cancel живого рана, прямое исполнение A3 (без заявок), rate-limit 429, fail-closed auth, deny-list команд (path-guard снят 30.10), FTS-backfill
   по text; 9. shutdown → turn'ы abandoned, telemetry.
Запуск: python scripts/smoke.py  (из корня репо)
"""
import os
import sys
import tempfile
import threading
import time
import urllib.request
import json
import asyncio
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="noosphere-smoke-")
os.environ["NOOSPHERE_CONFIG"] = os.path.join(TMP, "config.yaml")

CONFIG = f"""
server: {{host: 127.0.0.1, port: 8399, token: "", tz_offset_hours: 4,
          vault_key_file: {json.dumps(TMP + "/vault.key")},
          rate_limit: {{enabled: true, rpm: 100000, burst: 5000}}}}
data_dir: {TMP!r}
channels:
  alibaba: {{kind: alibaba, base_url: http://127.0.0.1:1, api_key_file: "", enabled: true}}
  ollama: {{kind: ollama, base_url: http://127.0.0.1:1, enabled: false}}
  zen: {{kind: zen, base_url: http://127.0.0.1:1, api_key_file: "", enabled: true}}
router: {{max_tokens: 128, limits: {{local: 0, base: 0, mid: 500, deep: 60, max: 20}},
          roles: {{judge: {{channel: zen, model: role-judge-a,
                            failover: [{{channel: alibaba, model: role-judge-b}}]}}}}}}
alerts: {{transport: log, sla_minutes: 30, digest_minutes: 60}}
antiloop: {{budget_warn_pct: 70, budget_crit_pct: 90, max_repeat_fingerprint: 3, max_water_cycles: 4}}
tools: {{stt_url: http://127.0.0.1:1/transcribe}}
route: {{enabled: true, tier: base, max_tokens: 128, prefilter: true}}
devrun: {{execute: false}}
projects: {{seed: [{{name: СмоукСид, keywords: "смоуксид"}}]}}
"""
with open(os.environ["NOOSPHERE_CONFIG"], "w", encoding="utf-8") as f:
    f.write(CONFIG)

import uvicorn  # noqa: E402

PORT = 8399
BASE = f"http://127.0.0.1:{PORT}"

# Бюджеты статического префикса по контекстам, знаков. Считаются только куски,
# которыми владеет код: системный блок агента/исполнителя, префлайт, JSON
# инструментов, шапка и хвост списка MCP, шапка блока памяти и каталог скилов
# худшего случая. Правила рабочего места (rules.compose, 2188 зн на бою) и
# содержимое памяти — данные владельца, в бюджет не входят. Замер 02.11 на бою
# (scripts/measure_prefix.py и scripts/probe_prefix.py): пустой чат-ход стоил
# 4951 prompt-токен при префиксе chat_tools 14 838 знаков, после резки —
# 11 121 знак и 3686 токенов тем же замером. Поднимать бюджет можно только
# вместе с объяснением, почему текст стал длиннее.
PREFIX_BUDGETS = {"chat_tools": 7300, "bg_no_shell": 6100,
                  "devexec": 5000, "skills_1c": 23200}


def req(method, path, body=None):
    r = urllib.request.Request(BASE + path, method=method,
                               data=json.dumps(body).encode() if body is not None else None,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return resp.status, json.loads(resp.read().decode())


def req_raw(method, path, data: bytes):
    """Сырые байты — так ходят чанки вложений."""
    r = urllib.request.Request(BASE + path, method=method, data=data,
                               headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return resp.status, resp.read()


def get_raw(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as resp:
        return resp.status, resp.read()


def req_status(method, path, body=None, raw: bytes | None = None):
    """Запрос без исключения на HTTP-ошибках: (status, json|None)."""
    import urllib.error
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(BASE + path, method=method, data=data,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            payload = resp.read().decode()
            return resp.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
        try:
            return e.code, json.loads(payload)
        except Exception:
            return e.code, None


def ws_exchange(payload: dict, max_frames: int = 20):
    """Минимальный WS-клиент на stdlib: handshake + текстовые фреймы.
    Возвращает список JSON-сообщений сервера до turn_end."""
    import base64
    import socket
    s = socket.create_connection(("127.0.0.1", PORT), timeout=30)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET /v1/ws HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError(f"ws handshake оборвался: {resp[:200]!r}")
        resp += chunk
    assert resp.split(b"\r\n")[0].startswith(b"HTTP/1.1 101"), resp[:200]

    def send_text(msg: str) -> None:
        data = msg.encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        header += mask
        s.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("ws closed")
            buf += chunk
        return buf

    def recv_msg():
        while True:
            b1, b2 = _read(2)
            opcode, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = int.from_bytes(_read(2), "big")
            elif n == 127:
                n = int.from_bytes(_read(8), "big")
            data = _read(n)
            if opcode == 1:
                return json.loads(data.decode())
            if opcode in (8, 9, 10):
                continue

    send_text(json.dumps(payload, ensure_ascii=False))
    out = []
    for _ in range(max_frames):
        m = recv_msg()
        out.append(m)
        if m.get("type") == "turn_end":
            break
    s.close()
    return out


def main():
    from server.main import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            st, h = req("GET", "/health")
            break
        except Exception:
            time.sleep(0.2)
    assert h["status"] == "ok", h
    st, page = get_raw("/")
    assert st == 200 and "<title>Ноосфера</title>".encode() in page, (st, page[:100])
    assert b'id="mobileMenu"' in page
    print("1. health + web client OK", h["version"])

    st, chat = req("POST", "/v1/chat", {"task_class": "chat", "prompt": "привет"})
    assert st == 200 and chat["ok"] is False, chat
    sid = chat["session_id"]
    assert sid and chat["turn_no"] == 1
    print("2. /v1/chat на мёртвых каналах OK:", chat["err"])

    st, sess = req("GET", f"/v1/sessions/{sid}")
    assert sess["id"] == sid and sess["status"] == "active", sess
    st, lst = req("GET", "/v1/sessions")
    me = next(s for s in lst if s["id"] == sid)
    # v1.1: state/queue_len присутствуют; каналы мёртвы → последний ход ok=false → error
    assert me["queue_len"] == 0 and me["state"] == "error", me
    st, res = req("POST", f"/v1/sessions/{sid}/resume")
    assert res["session_id"] == sid
    print("3. sessions OK (state:", me["state"], ")")

    st, q = req("GET", "/v1/quota")
    assert isinstance(q, list)
    st, b = req("GET", "/v1/burn?days=1")
    assert isinstance(b, list) and any(r["channel"] == "alibaba" for r in b), b
    st, a = req("GET", "/v1/alerts")
    assert isinstance(a, list)
    print("4. quota/burn/alerts OK (burn каналов:", len(b), ")")

    # 5. очередь: сессия искусственно занята → chat встаёт в очередь, steer обгоняет
    from server import main as smain
    smain.BUSY.add(sid)
    st, q1 = req("POST", "/v1/chat", {"session_id": sid, "prompt": "обычный в очередь"})
    assert q1.get("queued") is True and q1.get("position") == 1, q1
    st, q2 = req("POST", "/v1/chat", {"session_id": sid, "prompt": "стир второй", "steer": True})
    assert q2.get("queued") is True and q2.get("position") == 1, q2
    st, ql = req("GET", f"/v1/queue?session_id={sid}")
    active = [r for r in ql if r["status"] == "queued"]
    assert len(active) == 2, ql
    kinds = {r["kind"] for r in active}
    assert kinds == {"queue", "steer"}, kinds
    for r in active:
        st, c = req("POST", f"/v1/queue/{r['id']}/cancel")
        assert c.get("ok") is True, c
    st, ql2 = req("GET", f"/v1/queue?session_id={sid}")
    assert all(r["status"] == "cancelled" for r in ql2), ql2
    st, lst2 = req("GET", "/v1/sessions")
    me2 = next(s for s in lst2 if s["id"] == sid)
    assert me2["queue_len"] == 0 and me2["state"] == "busy", me2  # BUSY ещё держим
    smain.BUSY.discard(sid)
    print("5. queue/steer/cancel OK")

    # 6. FTS-поиск: prompt хода из шага 2 в индексе даже при провале канала
    from urllib.parse import quote
    st, sr = req("GET", "/v1/search?q=" + quote("привет"))
    assert any(r["session_id"] == sid for r in sr), sr
    hit = next(r for r in sr if r["session_id"] == sid)
    assert "<b>" in hit["snippet"], hit
    st, sr_empty = req("GET", "/v1/search?q=")
    assert sr_empty == [], sr_empty
    print("6. search OK, snippet:", hit["snippet"])

    # 7. вложения: init → 2 чанка → done → GET отдаёт байты; chat с attachments
    blob = b"hello noosphere attachment chunk test 0123456789"
    st, u = req("POST", "/v1/uploads/init",
                {"name": "note.txt", "size": len(blob), "mime": "text/plain", "session_id": sid})
    uid = u["upload_id"]
    half = len(blob) // 2
    st, c1 = req_raw("PUT", f"/v1/uploads/{uid}/chunk?offset=0", blob[:half])
    assert json.loads(c1)["received"] == half, c1
    st, c2 = req_raw("PUT", f"/v1/uploads/{uid}/chunk?offset={half}", blob[half:])
    assert json.loads(c2)["received"] == len(blob), c2
    st, d = req("POST", f"/v1/uploads/{uid}/done")
    assert d["ok"] and d["size"] == len(blob), d
    st, body = get_raw(f"/v1/uploads/{uid}")
    assert body == blob, body
    # чат — в НОВУЮ сессию: у sid уже 4+ провальных хода подряд, сработал бы
    # treading-water и ход не создался бы (это штатная защита, не баг)
    st, ch2 = req("POST", "/v1/chat",
                  {"prompt": "разбери вложение", "attachments": [uid]})
    sid2 = ch2["session_id"]
    assert sid2 and "err" in ch2, ch2  # каналы мёртвы, но валидация прошла
    st, sess2 = req("GET", f"/v1/sessions/{sid2}")
    last = sess2["turns"][-1]["snapshot"]
    assert last["attachments"] == [{"upload_id": uid, "name": "note.txt", "mime": "text/plain"}], last
    print("7. uploads OK, байт:", d["size"])

    # 8. расписания: create (next_run в будущем) → toggle → delete
    from server.db import now_iso
    st, sc = req("POST", "/v1/schedules",
                 {"name": "smoke-tick", "prompt": "тик", "interval_minutes": 1})
    scid = sc["id"]
    st, sl = req("GET", "/v1/schedules")
    row = next(r for r in sl if r["id"] == scid)
    assert row["next_run"] >= now_iso() and row["enabled"] == 1, row
    st, tg = req("POST", f"/v1/schedules/{scid}/toggle")
    assert tg["enabled"] is False, tg
    st, dl = req("DELETE", f"/v1/schedules/{scid}")
    assert dl.get("ok") is True, dl
    st, sl2 = req("GET", "/v1/schedules")
    assert not any(r["id"] == scid for r in sl2), sl2
    print("8. schedules OK")

    # 8b. инструменты агента — офлайн, без сети и модели
    if sys.platform == "win32":
        # uvicorn на Windows ставит selector-loop, а subprocess нужен proactor
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    from server import tools as srv_tools
    specs = srv_tools.openai_specs()
    assert {"shell_exec", "read_file", "write_file", "web_fetch"} <= {
        s["function"]["name"] for s in specs}, specs
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "t1", "function": {"name": "shell_exec",
                                  "arguments": json.dumps({"command": "echo smoke-tools"})}}))
    assert "smoke-tools" in out, out
    tmpf = Path(tempfile.gettempdir()) / "noosphere-smoke-tool.txt"
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "t2", "function": {"name": "write_file",
                                  "arguments": json.dumps({"path": str(tmpf), "content": "агент"})}}))
    assert "записано" in out, out
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "t3", "function": {"name": "read_file",
                                  "arguments": json.dumps({"path": str(tmpf)})}}))
    assert out == "агент", out
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "t4", "function": {"name": "нет_такого", "arguments": "{}"}}))
    assert "неизвестный инструмент" in out, out
    print("8b. tools OK (shell/write/read/unknown)")

    # 8c. терминал держит состояние, PTY и интерактив + прогоны (runs) — офлайн
    # терминал нельзя держать через границу asyncio.run (процесс привязан к
    # петле, которая закрылась) — все команды в одной петле, как на сервере.
    async def _term_checks():
        ctx = {"session_id": "smoke-term"}

        async def call(call_id, **args):
            return await srv_tools.execute_tool_call(
                {"id": call_id, "function": {"name": "terminal_exec",
                                              "arguments": json.dumps(args)}}, ctx=ctx)

        out = await call("t5", command="cd /tmp && export NS_SMOKE=42")
        assert "rc=" not in out.split("\n")[0], out
        out = await call("t6", command="pwd && echo $NS_SMOKE")
        assert "/tmp" in out and "42" in out, out
        out = await call("t7", command="printf 'abc\\n' | tr 'a-z' 'A-Z' > /tmp/noosphere-smoke-terminal.txt && grep ABC /tmp/noosphere-smoke-terminal.txt")
        assert "ABC" in out, out
        if sys.platform == "darwin":
            out = await call("t8", command="test -t 0 && test -t 1 && echo PTY_OK")
            assert "PTY_OK" in out, out
            out = await call("t9", command="read -n 1 NS_KEY; printf 'INPUT:%s\\n' \"$NS_KEY\"", timeout=1)
            assert "[процесс продолжается" in out, out
            out = await call("t10", input="Z", timeout=5)
            assert "INPUT:Z" in out and "[процесс продолжается" not in out, out
            out = await call("t11", command="sleep 2", timeout=1)
            assert "[процесс продолжается" in out, out
            out = await call("t12", poll=True, timeout=5)
            assert "[процесс продолжается" not in out, out
            out = await call("t13", close=True)
            assert out == "терминал закрыт", out
            summaries = srv_tools.get_db().execute(
                "SELECT args_summary FROM tool_audit WHERE session_id='smoke-term'"
                " AND tool='terminal_exec'").fetchall()
            stdin_rows = [r["args_summary"] for r in summaries
                          if r["args_summary"].startswith("stdin")]
            assert stdin_rows == ["stdin <1 chars>"], stdin_rows
    asyncio.run(_term_checks())
    st, run = req("POST", "/v1/runs", {"prompt": "smoke-прогон"})
    assert st == 201 and run.get("run_id"), (st, run)
    rid = run["run_id"]
    for _ in range(60):
        st, row = req("GET", f"/v1/runs/{rid}")
        if row["status"] in ("done", "error", "canceled"):
            break
        time.sleep(1)
    assert row["status"] == "error", row  # каналы мёртвы — честный error, не висок
    st, rows = req("GET", "/v1/runs")
    assert any(r["id"] == rid for r in rows), rows
    st, late = req("POST", f"/v1/runs/{rid}/cancel")
    assert late.get("ok") is False, late  # завершённый прогон не отменить
    print("8c. terminal/runs OK (cwd+env, PTY/input/poll/close, прогон error)")

    # 8d. батч 4: правила, память, пул аккаунтов — офлайн
    st, rule = req("POST", "/v1/rules", {"name": "smoke", "text": "ответ всегда по-русски"})
    assert st == 201 and rule.get("id"), (st, rule)
    rid_rule = rule["id"]
    st, rlist = req("GET", "/v1/rules")
    assert any(r["id"] == rid_rule and r["enabled"] == 1 for r in rlist), rlist
    from server import rules as srv_rules
    assert "ответ всегда по-русски" in srv_rules.compose(), srv_rules.compose()
    st, tg = req("POST", f"/v1/rules/{rid_rule}/toggle")
    assert tg["enabled"] is False, tg
    assert srv_rules.compose() == "", "выключенное правило попало в compose"
    st, dl = req("DELETE", f"/v1/rules/{rid_rule}")
    assert dl.get("ok") is True, dl
    from server import memory as srv_memory
    mid = srv_memory.save("smoke-заметка про кедр", tags="smoke")
    srv_memory.save("совсем другая запись", tags="smoke")
    found = srv_memory.search("кедр")
    assert len(found) == 1 and found[0]["id"] == mid, found
    from server import accounts as srv_accounts
    from server.config import ChannelCfg
    d1 = Path(tempfile.mkdtemp())
    (d1 / "k1").write_text("KEY-1", encoding="utf-8")
    (d1 / "k2").write_text("KEY-2", encoding="utf-8")
    ch = ChannelCfg(name="smoke-ch", kind="alibaba",
                    accounts=[{"api_key_file": str(d1 / "k1")},
                              {"api_key_file": str(d1 / "k2")}])
    assert srv_accounts.api_key(ch) == "KEY-1", srv_accounts.api_key(ch)
    nxt = srv_accounts.rotate(ch)
    assert nxt == 1 and srv_accounts.api_key(ch) == "KEY-2", (nxt, srv_accounts.api_key(ch))
    assert srv_accounts.rotate(ch) is None, "оба на cooldown — rotate обязан вернуть None"
    ch1 = ChannelCfg(name="smoke-solo", kind="alibaba")
    assert srv_accounts.rotate(ch1) is None, "один аккаунт — rotate = None"
    # 30.10: proxy-поле канала (фильтр маршрута дропает OpenSSL TLS к *.kimi.com)
    import inspect as _inspect
    from server import pool as _pool
    chp = ChannelCfg(name="smoke-proxy", kind="kimi", proxy="http://127.0.0.1:1")
    assert chp.proxy == "http://127.0.0.1:1", chp
    assert "proxy" in _inspect.signature(srv_accounts._refresh_kimi).parameters
    assert _pool._client_for("") is _pool.CLIENT
    assert _pool._client_for("http://127.0.0.1:1") is not _pool.CLIENT
    print("8d. rules/memory/accounts/proxy OK")

    # 8e. vault: секрет не светится в поиске, upsert не плодит дубли
    from server import vault as srv_vault
    vid = srv_vault.upsert("ssh", "smoke-host", host="smoke@10.0.0.1", login="smoke",
                           secret="s3cr3t-smoke", notes="тестовая запись", tags="smoke")
    found = srv_vault.search("smoke-host")
    assert len(found) == 1 and found[0]["has_secret"] == 1, found
    assert "secret" not in found[0] or found[0].get("secret") != "s3cr3t-smoke", found
    # F11.3: notes и secret_file не покидают публичную выдачу (только vault_get)
    assert "notes" not in found[0] and "secret_file" not in found[0], found
    st, vlist = req("GET", "/v1/vault?q=smoke-host")
    assert "notes" not in vlist[0] and "secret_file" not in vlist[0], vlist
    vid2 = srv_vault.upsert("ssh", "smoke-host", host="smoke@10.0.0.2",
                            notes="обновлено", tags="smoke")
    assert vid2 == vid, (vid, vid2)  # upsert, не дубль
    row = srv_vault.get(vid)
    assert row["secret"] == "s3cr3t-smoke" and row["host"] == "smoke@10.0.0.2", row
    # секрет не затирается пустым при upsert без secret (CASE в upsert)
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "v1", "function": {"name": "vault_get", "arguments": json.dumps({"id": vid})}}))
    assert "reveal=true" in out and "s3cr3t-smoke" not in out, out
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "v2", "function": {"name": "vault_get",
                                  "arguments": json.dumps({"id": vid, "reveal": True})}}))
    assert "s3cr3t-smoke" in out, out
    st, vs = req("GET", "/v1/vault?q=smoke-host")
    assert any(r["id"] == vid for r in vs), vs
    assert srv_vault.delete(vid) is True
    print("8e. vault OK (upsert, поиск без секрета, reveal)")

    # 8f. аудит r2: REST-контракты списков, tool_audit, джейл, deny-list
    for path in ("/v1/vault", "/v1/rules", "/v1/runs", "/v1/memory",
                 "/v1/tool_audit", "/v1/acceptance"):
        st, _ = req("GET", path)  # 500/404 = падение контракта (урок /v1/vault)
        assert st == 200, (path, st)
    st, acc = req("GET", "/v1/acceptance")
    assert acc.get("started") is False, acc  # старт только решением владельца
    st, audit = req("GET", "/v1/tool_audit?limit=30")
    assert any(r["tool"] == "shell_exec" for r in audit), audit  # следы 8b есть
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "j1", "function": {"name": "read_file",
                                  "arguments": json.dumps({"path": "~/.ssh/id_ed25519"})}}))
    assert "вне джейла" in out, out
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "j2", "function": {"name": "write_file",
                                  "arguments": json.dumps({"path": "~/.secrets/x", "content": "y"})}}))
    assert "вне джейла" in out, out
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "j3", "function": {"name": "shell_exec",
                                  "arguments": json.dumps({"command": "sudo ls /root"})}}))
    assert "запрещённая команда" in out, out
    st, audit2 = req("GET", "/v1/tool_audit?limit=3")
    assert any(r["note"] == "refuse" for r in audit2), audit2  # отказы журналируются
    print("8f. REST/tool_audit/jail/deny-list OK")

    # 8g. UX-пакет: папки/закрепление сессий, STT-контракт (ОФЛАЙН, G8: stt_url —
    # заглушка 127.0.0.1:1, проверяем контракт ошибки, боевой CT106 не дёргаем)
    st, f = req("POST", f"/v1/sessions/{sid2}/folder", {"folder": "smoke-папка"})
    assert f.get("ok") is True, f
    st, rows = req("GET", "/v1/sessions")
    row = next(r for r in rows if r["id"] == sid2)
    assert row["folder"] == "smoke-папка" and row["pinned"] is False, row
    st, p = req("POST", f"/v1/sessions/{sid2}/pin")
    assert p["pinned"] is True, p
    st, p = req("POST", f"/v1/sessions/{sid2}/pin")
    assert p["pinned"] is False, p
    st, f = req("POST", f"/v1/sessions/{sid2}/folder", {"folder": ""})
    st, rows = req("GET", "/v1/sessions")
    assert next(r for r in rows if r["id"] == sid2)["folder"] is None
    # минимальный корректный WAV-заголовок (RIFF/WAVE) — проходит валидацию,
    # апстрим мёртв → контракт ошибки {ok:false, err:"stt недоступен"}
    wav = b"RIFF" + (36).to_bytes(4, "little") + b"WAVE" + b"fmt " + b"\x00" * 64
    st, sresp = req_raw("POST", "/v1/stt", wav)
    sjson = json.loads(sresp.decode())
    assert sjson["ok"] is False and "stt недоступен" in sjson["err"], sjson
    print("8g. folders/pin/stt OK (офлайн-заглушка)")

    # 8h. батч 6: единая точка входа — route-классификатор, проекты, анализатор
    from server import projects as srv_projects
    from server import run_quality
    names = {p["name"] for p in srv_projects.list_projects()}
    assert {"Ноосфера", "Личное", "СмоукСид"} <= names, names
    st, r1 = req("POST", "/v1/route", {"prompt": "привет, что думаешь о погоде"})
    assert st == 200 and r1["kind"] == "chat" and r1["prefilter"] is True, r1
    assert r1["threshold"] > 0, r1
    st, r2 = req("POST", "/v1/route", {"prompt": "сделай отчёт по продажам"})
    assert r2["kind"] == "run" and r2["prefilter"] is True, r2
    # нейтральный текст с ключевым словом: LLM мёртв → fallback run с err,
    # проект — по ключевым словам (бесплатный сигнал без LLM)
    st, r3 = req("POST", "/v1/route", {"prompt": "что-то там с накладными по смоуксиду"})
    assert r3["kind"] == "run" and r3["prefilter"] is False and r3["err"], r3
    assert r3["project"] and r3["project"]["name"] == "СмоукСид", r3
    assert r3["confidence"] == 0.0, r3
    # каждая классификация — строка route_log
    from server.db import get_db as _gdb
    nlog = _gdb().execute("SELECT COUNT(*) c FROM route_log").fetchone()["c"]
    assert nlog >= 3, nlog
    # саморастущий проект: find_or_create создаёт auto=1 с именем как есть
    auto = srv_projects.find_or_create("СмоукПроект")
    assert auto["auto"] == 1 and auto["name"] == "СмоукПроект", auto
    assert srv_projects.find_or_create("смоукпроект")["id"] == auto["id"]  # case-insens
    # override — правка пользователя пишется
    st, ov = req("POST", f"/v1/route/{r3['log_id']}/override",
                 {"kind": "chat", "project_id": auto["id"]})
    assert st == 200 and ov.get("ok") is True, ov
    row = _gdb().execute("SELECT * FROM route_log WHERE id=?",
                         (r3["log_id"],)).fetchone()
    assert row["override"] == 1 and row["override_kind"] == "chat", dict(row)
    assert row["override_project_id"] == auto["id"], dict(row)
    import urllib.error
    try:
        req("POST", f"/v1/route/{r3['log_id']}/override", {"kind": "нет"})
        raise AssertionError("override с bad kind обязан дать 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400, e.code
    st, pl = req("GET", "/v1/projects")
    assert st == 200 and any(p["name"] == "СмоукПроект" for p in pl), pl
    st, np_ = req("POST", "/v1/projects", {"name": "СмоукДва", "keywords": "смоукдва"})
    assert st == 201 and np_["keywords"] == "смоукдва", np_
    st, rn = req("POST", f"/v1/projects/{np_['id']}/rename", {"name": "СмоукТри"})
    assert rn["name"] == "СмоукТри", rn
    # ночной анализатор: прямой вызов не падает на малых данных
    summary = run_quality.run_analysis()
    assert "[route_analysis]" in summary, summary
    print("8h. route/projects/override/analysis OK; классификаций в логе:", nlog)

    # 8i. WS /v1/ws: handshake + один ход (каналы мёртвы → ok:false, но
    # события turn_start/turn_end приходят; G8). Авторизации по токену нет
    # (smoke-конфиг с пустым токеном) — проверяем сам канал.
    msgs = ws_exchange({"prompt": "ws-проверка связи", "task_class": "chat"})
    types = [m.get("type") for m in msgs]
    assert "turn_start" in types and "turn_end" in types, msgs
    print("8i. WS /v1/ws OK (", " → ".join(types), ")")

    # 8j. alerts: ack по REST, статусы list (G2: без параметра — активные;
    # недоставленный crit подхватывается check_sla и уходит на ретрай)
    from server import alerts as srv_alerts
    aid = srv_alerts.send("warning", "smoke", "smoke warning для ack")
    st, ack1 = req("POST", f"/v1/alerts/{aid}/ack")
    assert ack1.get("ok") is True, ack1
    st, _ = req_status("POST", f"/v1/alerts/{aid}/ack")
    assert st == 404, st  # повторный ack — 404
    # недоставленный crit (эмуляция упавшей доставки) — ретрай через check_sla
    from server.db import get_db as _gdb2, now_iso as _now
    import datetime as _dt
    dbx = _gdb2()
    old_try = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=3)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    cur = dbx.execute(
        "INSERT INTO alerts (ts, kind, severity, text, status, meta)"
        " VALUES (?,?,?,?,?,?)",
        (_now(), "smoke-broken", "crit", "недоставленный crit", "new",
         json.dumps({"delivery_error": "ssh rc=255", "delivery_attempts": 1,
                     "last_delivery_try": old_try})))
    dbx.commit()
    broken_id = cur.lastrowid
    srv_alerts.check_sla()  # transport=log → ретрай успешен → sent
    row = dbx.execute("SELECT status FROM alerts WHERE id=?", (broken_id,)).fetchone()
    assert row["status"] == "sent", dict(row)
    st, lst_active = req("GET", "/v1/alerts")           # без status: new+sent+escalated
    assert any(r["id"] == broken_id for r in lst_active), lst_active
    assert not any(r["id"] == aid for r in lst_active)  # acked не в активных
    st, lst_acked = req("GET", "/v1/alerts?status=acked")
    assert any(r["id"] == aid for r in lst_acked), lst_acked
    st, lst_all = req("GET", "/v1/alerts?status=all&limit=100")
    assert any(r["id"] == aid for r in lst_all), "status=all должен включать acked"
    print("8j. alerts ack/status/backoff OK")

    # 8k. vault REST + шифрование at-rest (G1.5) + маскировка reveal (в снапшот)
    st, v = req("POST", "/v1/vault",
                {"kind": "token", "name": "smoke-rest", "secret": "MASK-ME-123",
                 "notes": "smoke"})
    assert st == 201 and v.get("id"), (st, v)
    vid3 = v["id"]
    raw = dbx.execute("SELECT secret FROM vault WHERE id=?", (vid3,)).fetchone()["secret"]
    assert raw != "MASK-ME-123" and raw.startswith("gAAAA"), "секрет лёг открытым текстом"
    row = srv_vault.get(vid3)
    assert row["secret"] == "MASK-ME-123", "расшифровка сломана"
    ctx = {"session_id": "smoke-mask", "reveal_secrets": []}
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "v3", "function": {"name": "vault_get",
                                  "arguments": json.dumps({"id": vid3, "reveal": True})}},
        ctx=ctx))
    assert "MASK-ME-123" in out and ctx["reveal_secrets"] == ["MASK-ME-123"], out
    from server import sessions as srv_sessions
    sidm = srv_sessions.create_session("arman", "test", "smoke")
    tn = srv_sessions.append_turn(sidm, {"prompt": "покажи"})
    srv_sessions.complete_turn(sidm, tn, 0,
                               {"prompt": "покажи", "text": "токен MASK-ME-123 готов"},
                               ctx["reveal_secrets"])
    snap = srv_sessions.get_session(sidm)["turns"][-1]["snapshot"]
    assert "MASK-ME-123" not in json.dumps(snap, ensure_ascii=False), snap
    assert "***" in snap["text"], snap
    st, dl = req("DELETE", f"/v1/vault/{vid3}")
    assert dl.get("ok") is True, dl
    st, _ = req_status("DELETE", f"/v1/vault/{vid3}")
    assert st == 404, st
    # F6-хвост: REST-мутации vault журналируются в tool_audit. rc унифицирован с
    # остальными инструментами (правка 01.11): 0=успех, не-0=провал; HTTP-статус
    # переехал в args_summary (upsert [http 201], delete [http 200], повторный 404)
    st, va = req("GET", "/v1/tool_audit?limit=10")
    rest = [r for r in va if r["tool"] == "vault_rest"]
    assert any(r["rc"] == 0 and "token/smoke-rest" in r["args_summary"]
               and "[http 201]" in r["args_summary"] for r in rest), rest
    assert any(r["rc"] == 0 and f"id={vid3}" in r["args_summary"]
               and "[http 200]" in r["args_summary"] for r in rest), rest
    assert any(r["rc"] == 404 and r["note"] == "refuse" for r in rest), rest
    assert all("MASK-ME-123" not in (r["args_summary"] or "") for r in rest), rest
    print("8k. vault REST + at-rest шифрование + маскировка reveal + tool_audit OK")

    # 8l. acceptance (G4): run-ходы НЕ засчитываются, inline — засчитываются;
    # история стартов в acceptance_events
    st, acc = req("GET", "/v1/acceptance")
    assert acc.get("started") is False, acc
    st, r = req("POST", "/v1/acceptance/start")
    assert r["started"] is True and r["goal_days"] == 14, r
    from server import telemetry as srv_tel
    st, acc = req("GET", "/v1/acceptance")
    own0 = acc["days"][-1]["own_turns"]
    srv_tel.record_turn(srv_tel.TurnRec(user="arman", turn_kind="run", task_class="chat"))
    srv_tel.record_turn(srv_tel.TurnRec(user="scheduler", turn_kind="schedule",
                                        task_class="chat"))
    srv_tel.record_turn(srv_tel.TurnRec(user="bioclaude-x", task_class="external",
                                        channel="bioclaude:test"))
    st, acc = req("GET", "/v1/acceptance")
    row_t = acc["days"][-1]
    assert row_t["own_turns"] == own0, (own0, row_t)  # run/schedule не прибавили
    assert row_t["bioclaude_calls"] == 1 and row_t["counted"] is False, row_t
    srv_tel.record_turn(srv_tel.TurnRec(user="arman", turn_kind="inline",
                                        task_class="chat"))
    st, acc = req("GET", "/v1/acceptance")
    row_t = acc["days"][-1]
    assert row_t["own_turns"] == own0 + 1 and row_t["counted"] is False, row_t  # есть bioclaude
    dbx.execute("DELETE FROM turns WHERE channel='bioclaude:test'")
    dbx.commit()
    st, acc = req("GET", "/v1/acceptance")
    assert acc["days"][-1]["counted"] is True and acc["streak"] == 1, acc["days"][-1]
    st, r2 = req("POST", "/v1/acceptance/start")  # повторный старт = reset+start
    ev = [e["action"] for e in dbx.execute(
        "SELECT action FROM acceptance_events ORDER BY id").fetchall()]
    assert ev == ["start", "reset", "start"], ev
    dbx.execute("DELETE FROM acceptance_events")
    dbx.execute("DELETE FROM turns WHERE turn_kind IN ('run','schedule','inline')"
                " AND session_id IS NULL")
    dbx.commit()
    print("8l. acceptance OK (интерактив ≠ runs/scheduler, история стартов)")

    # 8m. quota/probe на мёртвых каналах: контракт ошибки, не падение
    st, probes = req("POST", "/v1/quota/probe")
    assert st == 200 and isinstance(probes, list) and probes, probes
    assert all("ok" in p and "channel" in p for p in probes), probes
    assert any(p["ok"] is False for p in probes), probes  # 127.0.0.1:1 мёртв
    print("8m. quota/probe на мёртвых каналах OK")

    # 8n. list-эндпоинты с параметрами
    st, rl = req("GET", "/v1/route_log?limit=2")
    assert st == 200 and len(rl) <= 2 and rl, rl
    from server.db import today_local as _today
    st, rl2 = req("GET", f"/v1/route_log?day={_today()}")
    assert st == 200 and any(r["id"] == r1["log_id"] for r in rl2), rl2
    st, ta = req("GET", f"/v1/tool_audit?limit=5&session_id=smoke-term")
    assert st == 200 and all(r["session_id"] == "smoke-term" for r in ta), ta
    st, ta2 = req("GET", "/v1/tool_audit?limit=1")
    assert "args_summary" in ta2[0] and "rc" in ta2[0] and "turn_no" in ta2[0], ta2
    st, runs1 = req("GET", "/v1/runs?limit=1")
    assert st == 200 and len(runs1) <= 1, runs1
    st, vault1 = req("GET", "/v1/vault?limit=1")
    assert st == 200, vault1
    print("8n. list-эндпоинты с параметрами OK")

    # 8o. негативы батча 6 + stt + uploads + живой ран
    st, _ = req_status("POST", "/v1/route/999999/override", {"kind": "chat"})
    assert st == 404, st
    st, _ = req_status("POST", f"/v1/projects/{np_['id']}/rename", {"name": ""})
    assert st == 400, st
    st, _ = req_status("POST", "/v1/projects/999999/rename", {"name": "x"})
    assert st == 404, st
    st, _ = req_status("POST", "/v1/route", {"prompt": "   "})
    assert st == 400, st
    from server.config import load_config as _lc
    _lc().route.enabled = False
    st, _ = req_status("POST", "/v1/route", {"prompt": "роут выключен"})
    assert st == 503, st
    _lc().route.enabled = True
    st, _ = req_status("POST", "/v1/stt", raw=b"")
    assert st == 400, st  # пустое аудио
    st, _ = req_status("POST", "/v1/stt", raw=b"NOPE-not-audio")
    assert st == 400, st  # нет сигнатуры
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    conn.putrequest("POST", "/v1/stt")
    conn.putheader("Content-Length", str(26 * 1024 * 1024))  # лимит ДО буферизации
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 413, resp.status
    resp.read()
    conn.close()
    # uploads 409: чанк не с того offset и повторная запись в done-вложение
    st, u2 = req("POST", "/v1/uploads/init", {"name": "neg.bin", "size": 10})
    st, _ = req_status("PUT", f"/v1/uploads/{u2['upload_id']}/chunk?offset=5", raw=b"xx")
    assert st == 409, st
    st, _ = req_status("PUT", f"/v1/uploads/{uid}/chunk?offset=0", raw=b"x")
    assert st == 409, st  # uid из шага 7 уже done
    st, _ = req_status("GET", "/v1/uploads/nonexistent-upload-id")
    assert st == 404, st
    # cancel ЖИВОГО рана (ран ждёт освобождения сессии в BUSY)
    st, ch3 = req("POST", "/v1/chat", {"prompt": "сессия под живой ран"})
    sidr = ch3["session_id"]
    smain.BUSY.add(sidr)
    st, run2 = req("POST", "/v1/runs", {"prompt": "живой ран", "session_id": sidr})
    rid2 = run2["run_id"]
    for _ in range(40):
        st, row2 = req("GET", f"/v1/runs/{rid2}")
        if row2["status"] == "running":
            break
        time.sleep(0.5)
    assert row2["status"] == "running", row2
    st, c2 = req("POST", f"/v1/runs/{rid2}/cancel")
    assert c2.get("ok") is True, c2
    for _ in range(40):
        st, row2 = req("GET", f"/v1/runs/{rid2}")
        if row2["status"] == "canceled":
            break
        time.sleep(0.5)
    assert row2["status"] == "canceled", row2
    smain.BUSY.discard(sidr)
    print("8o. негативы (override/rename/route/stt/uploads/cancel) OK")

    # 8p. A3 исполняется напрямую (заявки approvals удалены решением владельца
    # 2026-10-17): ssh/git push идут на исполнение, маркера requires_approval
    # нет, REST /v1/approvals снят (404). git push — только через -C /tmp:
    # рабочий каталог smoke — живой репозиторий, настоящий push недопустим.
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "a1", "function": {"name": "shell_exec",
                                  "arguments": json.dumps({"command": "ssh smoke-node.invalid true", "timeout": 15})}}))
    assert "requires_approval" not in out and out.startswith("rc="), out
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "a2", "function": {"name": "shell_exec",
                                  "arguments": json.dumps({"command": "git -C /tmp push origin main"})}}))
    assert "requires_approval" not in out and out.startswith("rc="), out
    st, _ = req_status("GET", "/v1/approvals?status=pending")
    assert st == 404, st
    print("8p. A3 исполняется напрямую, /v1/approvals снят OK")

    # 8q. rate-limit (G1.4): при превышении — 429 (лимит подкручиваем вживую,
    # весь остальной smoke работал на огромном лимите из конфига)
    _lc().server.rate_limit = {"enabled": True, "rpm": 6, "burst": 2}
    codes = [req_status("GET", "/v1/quota")[0] for _ in range(8)]
    assert 429 in codes, codes
    _lc().server.rate_limit = {"enabled": True, "rpm": 100000, "burst": 5000}
    print("8q. rate-limit 429 OK")

    # 8r. fail-closed _auth (G1.4): пустой токен + non-loopback → отказ старта
    from types import SimpleNamespace
    from server.main import _fail_closed_check
    try:
        _fail_closed_check(SimpleNamespace(
            server=SimpleNamespace(host="0.0.0.0", token="", token_source="")))
        raise AssertionError("fail-open при пустом токене и 0.0.0.0!")
    except RuntimeError:
        pass
    _fail_closed_check(SimpleNamespace(
        server=SimpleNamespace(host="127.0.0.1", token="", token_source="")))
    _fail_closed_check(SimpleNamespace(
        server=SimpleNamespace(host="0.0.0.0", token="x", token_source="file")))
    from server.main import _auth
    _lc().server.lan_no_auth = True
    _lc().server.host = "0.0.0.0"
    _fail_closed_check(_lc())
    _auth(SimpleNamespace(headers={}, client=SimpleNamespace(host="192.168.0.50")))
    try:
        _auth(SimpleNamespace(headers={}, client=SimpleNamespace(host="8.8.8.8")))
        raise AssertionError("внешний клиент прошёл без токена при lan_no_auth")
    except Exception as e:
        assert getattr(e, "status_code", None) == 401, e
    # Цепочка адресов (фикс 01.11): наличие forwarded-заголовка само по себе
    # больше НЕ делает клиента внешним — tailscale funnel ставит X-Forwarded-For
    # всегда, и телефон владельца внутри своего же tailnet получал 401 на всё.
    # Доверяем, только когда КАЖДОЕ звено (peer + X-Forwarded-For + X-Real-IP)
    # локальное; внешний адрес в любом звене → 401 без Bearer.
    for host, hdrs in (
        ("127.0.0.1", {}),                                         # loopback
        ("100.95.239.47", {}),                                     # tailnet мака
        ("100.95.239.47", {"x-forwarded-for": "100.77.218.52"}),   # funnel: телефон
        ("192.168.0.50", {"x-forwarded-for": "192.168.0.50, 10.0.0.7"}),
        ("127.0.0.1", {"x-forwarded-for": "[::1]:443"}),
        ("192.168.0.50", {"x-real-ip": "192.168.0.235"}),
    ):
        _auth(SimpleNamespace(headers=hdrs, client=SimpleNamespace(host=host)))

    def _must401(host, hdrs):
        try:
            _auth(SimpleNamespace(headers=hdrs, client=SimpleNamespace(host=host)))
        except Exception as e:                                     # noqa: BLE001
            assert getattr(e, "status_code", None) == 401, e
            return
        raise AssertionError(f"внешний клиент прошёл без токена: {host} {hdrs}")

    for host, hdrs in (
        ("127.0.0.1", {"x-forwarded-for": "8.8.8.8"}),             # внешний в цепочке
        ("100.95.239.47", {"x-forwarded-for": "100.77.218.52, 8.8.8.8"}),
        ("192.168.0.50", {"x-real-ip": "1.1.1.1"}),
        ("8.8.8.8", {}),
    ):
        _must401(host, hdrs)
    _lc().server.lan_no_auth = False
    _lc().server.host = "127.0.0.1"
    # path-guard снят решением владельца 30.10.2026 (ложные отказы на remote-путях
    # ssh-команд стоили дороже защиты). Бывшие deny-пути в команде больше не
    # отклоняются (пробы безопасные — echo ничего не читает и не запускает);
    # deny-list команд и джейл файловых инструментов остались и проверяются.
    for now_ok in ("echo ~/.config", "echo ~/.ssh/id_ed25519",
                   "echo sqlite3 ~/noosphere/data/noosphere.db .dump",
                   "echo bash /tmp/evil.sh", "echo python3 /tmp/x.py"):
        out = asyncio.run(srv_tools.execute_tool_call(
            {"id": "g", "function": {"name": "shell_exec",
                                     "arguments": json.dumps({"command": now_ok})}}))
        assert "запрещённая команда" not in out, (now_ok, out)
    for bad in ("sudo ls /root", "rm -rf /"):
        out = asyncio.run(srv_tools.execute_tool_call(
            {"id": "g1", "function": {"name": "shell_exec",
                                      "arguments": json.dumps({"command": bad})}}))
        assert "запрещённая команда" in out, (bad, out)
    # чтение вне allowlist (фолбэк «весь $HOME» убран)
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "g2", "function": {"name": "read_file",
                                  "arguments": json.dumps({"path": "~/some-random-file.txt"})}}))
    assert "вне джейла" in out, out
    print("8r. fail-closed auth + deny-list команд (path-guard снят) OK")

    # 8s. FTS-бэкфилл по text для ходов без prompt (G10/P1-8)
    sidb = srv_sessions.create_session("arman", "test", "smoke")
    dbx.execute("INSERT INTO session_turns (session_id, turn_no, ts, status, snapshot,"
                " tokens_spent) VALUES (?,?,?,?,?,0)",
                (sidb, 1, _now(), "done", json.dumps({"text": "уникальный бэкфилл-маяк"})))
    dbx.commit()
    from server import db as srv_db
    srv_db.backfill_fts()
    res = srv_db.search_turns("бэкфилл-маяк")
    assert any(r["session_id"] == sidb for r in res), res
    print("8s. FTS backfill по text OK")

    # 8t. заголовки сессий (v1.5): эвристика на мёртвых каналах, явный title,
    # rename 200/404/400
    time.sleep(1)  # _gen_title — fire-and-forget таска, даём ей отработать
    st, lst = req("GET", "/v1/sessions")
    me = next(s for s in lst if s["id"] == sid)
    assert me.get("title") and "привет" in me["title"], me  # эвристика от промпта
    st, chat2 = req("POST", "/v1/chat", {"task_class": "chat",
                                         "prompt": "ещё один диалог",
                                         "title": "  Тестовый   заголовок  "})
    assert st == 200 and chat2["session_id"], chat2
    sid2 = chat2["session_id"]
    st, lst = req("GET", "/v1/sessions")
    me2 = next(s for s in lst if s["id"] == sid2)
    assert me2.get("title") == "Тестовый заголовок", me2  # sanitize схлопнул пробелы
    st, r = req("POST", f"/v1/sessions/{sid2}/rename", {"title": "Новое имя сессии"})
    assert st == 200 and r["title"] == "Новое имя сессии", (st, r)
    st, _r = req_status("POST", "/v1/sessions/nesuschestvuyuschego/rename", {"title": "x"})
    assert st == 404, st
    st, _r = req_status("POST", f"/v1/sessions/{sid2}/rename", {"title": "   "})
    assert st == 400, st
    print("8t. titles OK (эвристика:", me["title"] + ")")

    # 8u. вложения v1.6: текст инлайнится, картинка без vision → честный
    # fallback в ответе хода; system-note о ране в истории origin-сессии
    from server import uploads as srv_uploads
    st, up = req("POST", "/v1/uploads/init",
                 {"name": "notes.txt", "size": 11, "mime": "text/plain"})
    uid = up["upload_id"]
    st, _b = req_status("PUT", f"/v1/uploads/{uid}/chunk?offset=0", raw=b"hello world")
    assert st == 200, st
    req("POST", f"/v1/uploads/{uid}/done")
    parts = srv_uploads.build_attachment_parts([uid])
    assert any("Вложение notes.txt" in b and "hello world" in b
               for b in parts["text_blocks"]), parts
    st, upi = req("POST", "/v1/uploads/init",
                  {"name": "px.png", "size": 4, "mime": "image/png"})
    uid2 = upi["upload_id"]
    req_status("PUT", f"/v1/uploads/{uid2}/chunk?offset=0", raw=b"\x89PNG")
    req("POST", f"/v1/uploads/{uid2}/done")
    parts2 = srv_uploads.build_attachment_parts([uid2])
    assert len(parts2["images"]) == 1 and parts2["images"][0]["mime"] == "image/png", parts2
    st, chat3 = req("POST", "/v1/chat", {"task_class": "chat", "prompt": "смотри скрин",
                                         "attachments": [uid2]})
    assert st == 200, chat3
    # каналы smoke-конфига без vision → честный fallback виден в ответе хода
    assert "vision-канал не настроен" in (chat3.get("err", "") + chat3.get("text", "")), chat3
    st, run = req("POST", "/v1/runs", {"prompt": "тестовый фоновый прогон",
                                       "session_id": sid})
    assert st == 201 and run.get("run_id"), run
    st, sess3 = req("GET", f"/v1/sessions/{sid}")
    notes = [(t.get("snapshot") or {}) for t in sess3["turns"]
             if (t.get("snapshot") or {}).get("kind") == "system-note"]
    assert notes and "прогон" in notes[-1].get("text", "").lower(), sess3["turns"]
    hist = srv_sessions.get_history(sid)
    assert any("фоновый прогон" in m["content"] for m in hist
               if m["role"] == "assistant"), hist
    print("8u. attachments v1.6 (текст инлайн, vision-fallback) + run system-note OK")

    # 8v. v1.7 (1С-контур): скилы, MCP-парсер, процессы, zen-проба, force_channel
    from server import skills as srv_skills, mcp as srv_mcp, processes as srv_proc
    # миграция v19: таблицы на месте
    tables = {r[0] for r in dbx.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"processes", "process_stages", "skill_state"} <= tables, tables
    # каталог скилов: временный каталог, кэш, toggle через REST, skill_load
    skdir = Path(tempfile.mkdtemp()) / "skills"
    (skdir / "1c-smoke").mkdir(parents=True)
    (skdir / "1c-smoke" / "SKILL.md").write_text(
        "---\nname: 1c-smoke\ndescription: Смоук-скил для проверки каталога. Вторая строка.\n---\n\n"
        "# Тело смоук-скила\nДелай раз. Делай два.\n", encoding="utf-8")
    (skdir / "plain").mkdir()
    (skdir / "plain" / "SKILL.md").write_text("без frontmatter\n", encoding="utf-8")
    _lc().skills.dir = str(skdir)
    cat = srv_skills.catalog()
    assert any(s["name"] == "1c-smoke" and "Смоук-скил" in s["desc"] and s["enabled"] == 1
               for s in cat), cat
    block = srv_skills.auto_block()
    assert "1c-smoke" in block and "skill_load" in block, block
    assert srv_skills.auto_block("проверь диск и свободную память") == ""
    assert "1c-smoke" in srv_skills.auto_block("доработай конфигурацию 1С")
    st, sl = req("GET", "/v1/skills")
    assert st == 200 and any(s["name"] == "1c-smoke" for s in sl), sl
    st, tg = req("POST", "/v1/skills/1c-smoke/toggle")
    assert tg["enabled"] is False, tg
    assert "1c-smoke" not in srv_skills.auto_block(), "выключенный скил попал в auto_block"
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "s1", "function": {"name": "skill_load",
                                  "arguments": json.dumps({"name": "1c-smoke"})}}))
    assert "Делай раз" in out, out
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "s2", "function": {"name": "skill_load",
                                  "arguments": json.dumps({"name": "несуществующий"})}}))
    assert "не найден" in out, out
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "s3", "function": {"name": "skill_list", "arguments": "{}"}}))
    assert "1c-smoke" in out and "[выключен]" in out, out
    st, _x = req_status("POST", "/v1/skills/no-such-skill/toggle")
    assert st == 404, st
    # MCP: SSE-парсер и склейка data-строк (офлайн, чистая функция)
    sse = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\ndata: \"result\":{}}\n\n" \
          "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"tools\":[]}}\n\n"
    payloads = srv_mcp.parse_sse(sse)
    assert len(payloads) == 2 and json.loads(payloads[0])["id"] == 1, payloads
    assert json.loads(payloads[1])["result"]["tools"] == [], payloads
    out = asyncio.run(srv_tools.execute_tool_call(
        {"id": "m1", "function": {"name": "mcp_tools",
                                  "arguments": json.dumps({"server": "не-настроен"})}}))
    assert "не настроен" in out, out
    # вердикт судьи: чистый JSON, markdown-обёртка, мусор
    v = srv_proc.parse_judge_verdict('{"verdict":"ACCEPT","issues":[],"summary":"годно"}')
    assert v and v["verdict"] == "ACCEPT", v
    v = srv_proc.parse_judge_verdict('текст вокруг ```json\n{"verdict":"NEEDS_FIXES","issues":["a"],"summary":"s"}\n``` хвост')
    assert v and v["verdict"] == "NEEDS_FIXES" and v["issues"] == ["a"], v
    assert srv_proc.parse_judge_verdict("никакого json") is None
    assert srv_proc.parse_judge_verdict('{"verdict":"MAYBE"}') is None
    # процессы REST: на мёртвом канале спеки стадия spec падает честно, не висок
    st, _x = req_status("POST", "/v1/processes", {"kind": "bogus", "task": "x"})
    assert st == 400, st
    st, _x = req_status("POST", "/v1/processes", {"kind": "dev_1c", "task": "  "})
    assert st == 400, st
    st, pr = req("POST", "/v1/processes", {"kind": "dev_1c", "task": "smoke-процесс"})
    assert st == 201 and pr.get("process_id"), (st, pr)
    pid = pr["process_id"]
    for _ in range(90):
        st, row = req("GET", f"/v1/processes/{pid}")
        if row["status"] in srv_proc.TERMINAL_STATUSES:
            break
        time.sleep(1)
    assert row["status"] == "error", row  # канал спеки мёртв (127.0.0.1:1) — честный error
    assert row["stages"] and row["stages"][0]["stage"] == "spec", row["stages"]
    assert row["stages"][0]["status"] == "error", row["stages"]
    # канал и класс стадии сверяем с объявлением ROLES, а не с именем провайдера:
    # спека переезжала zen → alibaba (20.10), тест не должен знать провайдера
    assert row["stages"][0]["channel"] == srv_proc.ROLES["spec"][0], row["stages"]
    assert row["stages"][0]["role"] == srv_proc.ROLES["spec"][3], row["stages"]
    st, plist = req("GET", "/v1/processes")
    assert any(r["id"] == pid for r in plist), plist
    st, c = req("POST", f"/v1/processes/{pid}/cancel")
    assert c.get("ok") is False, c  # завершённый процесс не отменить
    st, _x = req_status("GET", "/v1/processes/no-such-id")
    assert st == 404, st
    # force_channel: вызов идёт строго в zen (мёртв), эскалации по лестнице нет
    from server import pool as srv_pool
    res = asyncio.run(srv_pool.ask("code", "smoke", force_channel="zen",
                                   force_spec={"model": "mimo-v2.6-pro", "body": {}},
                                   force_no_escalate=True))
    assert res["ok"] is False and len(res["trace"]) == 1, res["trace"]
    assert res["trace"][0]["tier"] == "mid", res["trace"]  # класс code → тир mid, но канал zen
    # проба zen офлайн — честный контракт ошибки
    from server import quotas as srv_quotas
    r = srv_quotas.probe_zen(_lc().channel("zen"))
    assert r["ok"] is False and "unreachable" in str(r["raw"]), r
    print("8v. skills/mcp/processes/zen/force_channel OK")

    # п.19: вызовы без tools не должны нести tool-следы — станция за litellm
    # отвергает их с 400 UnsupportedParamsError (бой 28–29.10, 18 ходов)
    stripped = srv_pool._strip_tools_from_messages([
        {"role": "user", "content": "прочитай файл"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "данные файла"},
        {"role": "assistant", "content": "готово", "tool_calls": None},
        {"role": "user", "content": "итог?"}])
    assert all(m.get("role") != "tool" for m in stripped), stripped
    assert not any(m.get("tool_calls") for m in stripped), stripped
    assert any("данные файла" in str(m.get("content") or "") for m in stripped), stripped
    assert any("read_file" in str(m.get("content") or "") for m in stripped), stripped
    assert [m["role"] for m in stripped] == ["user", "assistant", "assistant", "user"], stripped
    print("8w. strip tool-истории для вызовов без tools OK")

    # 8x (31.10): серверный исполнитель прогонов. Главная регрессия — патч из
    # worktree ОБЯЗАН накладываться на дерево того же репозитория (последняя миля
    # самоправки ломалась именно здесь: base брали из форка GitLab, и apply
    # отвечал «patch does not apply»). Сеть и модель не нужны.
    from server import devexec as srv_devexec
    import subprocess as _spx
    src = Path(TMP) / "devexec-src"
    wt = Path(TMP) / "devexec-wt"
    src.mkdir(parents=True, exist_ok=True)
    GID = ["-c", "user.email=smoke@local", "-c", "user.name=smoke"]

    def _gx(args, cwd):
        r = _spx.run([srv_devexec.GIT, *args], cwd=str(cwd),
                     capture_output=True, text=True)
        assert r.returncode == 0, (args, r.stdout[-300:], r.stderr[-300:])
        return r.stdout.strip()

    _gx(["init", "-q", "-b", "main", str(src)], Path(TMP))
    (src / "a.txt").write_text("one\n", encoding="utf-8")
    _gx(GID + ["add", "-A"], src)
    _gx(GID + ["commit", "-qm", "init"], src)
    _gx(["worktree", "add", "--detach", str(wt), "main"], src)
    (wt / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (wt / "b.txt").write_text("новый файл\n", encoding="utf-8")
    patch_txt = srv_devexec.extract_patch(wt)
    assert "+++ b/a.txt" in patch_txt, patch_txt[:300]
    assert "+++ b/b.txt" in patch_txt, "новый файл не попал в патч"
    chk = _spx.run([srv_devexec.GIT, "apply", "--check", "--3way",
                    "--whitespace=nowarn"], cwd=str(src), input=patch_txt,
                   capture_output=True, text=True)
    assert chk.returncode == 0, (chk.stdout[-300:], chk.stderr[-300:])
    # состав инструментов исполнителя: без рекурсии и без под-агентов
    names = {(s.get("function") or {}).get("name") for s in srv_devexec._specs()}
    assert "devrun_request" not in names and "delegate" not in names, names
    assert {"read_file", "write_file", "shell_exec"} <= names, names
    assert "ТОЛЬКО внутри" in srv_devexec.EXEC_SYSTEM
    _gx(["worktree", "remove", "--force", str(wt)], src)
    print("8x. devexec: worktree → патч → apply --check OK")

    # 8y (31.10): автопатч — уборка мусора неудачного apply и тема коммита из
    # задачи. Оба дефекта найдены 31.10: client/web/index.html.rej в боевом
    # дереве отменял CI-деплой, а тема коммита брала первую строку отчёта модели
    # («devrun …: Изменён только README.md в worktree.»).
    from server import autopatch as srv_ap, db as srv_db, runs as srv_runs
    real_root = srv_ap.ROOT
    try:
        srv_ap.ROOT = src          # temp-репо из 8x, боевое дерево не трогаем
        (src / "web").mkdir(exist_ok=True)
        (src / "web" / "index.html.rej").write_text("мусор\n", encoding="utf-8")
        (src / "b.orig").write_text("мусор\n", encoding="utf-8")
        (src / "keep.txt").write_text("не мусор\n", encoding="utf-8")
        removed = srv_ap.clean_rejects()
        assert "web/index.html.rej" in removed, removed
        assert "b.orig" in removed, removed
        assert not (src / "web" / "index.html.rej").exists()
        assert not (src / "b.orig").exists()
        assert (src / "keep.txt").exists(), "убран посторонний файл"
        st = _gx(["status", "--porcelain"], src)
        assert ".rej" not in st and ".orig" not in st, st
    finally:
        srv_ap.ROOT = real_root
    # тема коммита: задача прогона важнее отчёта исполнителя
    rid_y = srv_runs.create("devrun",
                            {"task": "Починить поиск по памяти: склеиваются токены"})
    srv_db.get_db().execute("UPDATE runs SET status='done' WHERE id=?", (rid_y,))
    srv_db.get_db().commit()
    subj = srv_ap._subject(rid_y, "Изменён только README.md в worktree.",
                           ["README.md"])
    assert "Починить поиск по памяти" in subj, subj
    assert "worktree" not in subj, "тема снова из отчёта модели: " + subj
    assert subj.startswith(f"devrun {rid_y[:12]}: "), subj
    subj2 = srv_ap._subject("нет-такого-прогона", "",
                            ["server/pool.py", "README.md"])
    assert "2 файл" in subj2 and "pool.py" in subj2, subj2
    print("8y. autopatch: clean_rejects + тема коммита из задачи OK")

    # 8z (31.10): прогон, который сервис исполнял сам и потерял на перезапуске,
    # возвращается в очередь, а не хоронится ловцом сирот (регрессия переезда
    # исполнителя на сервер: деплой обрывал ход на середине).
    rid_z = srv_runs.create("devrun", {"task": "оборванный перезапуском"})
    srv_db.get_db().execute("UPDATE runs SET status='running' WHERE id=?",
                            (rid_z,))
    srv_db.get_db().commit()
    back = srv_devexec.requeue_interrupted()
    assert rid_z in back, back
    row_z = srv_db.get_db().execute("SELECT status, err FROM runs WHERE id=?",
                                    (rid_z,)).fetchone()
    assert row_z["status"] == "queued", dict(row_z)
    assert "возвращён в очередь" in (row_z["err"] or ""), dict(row_z)
    # running С патчем (класс B, бой c2a639d2 01.11): процесс умер между finish
    # и commit — на старте живых задач нет, поэтому тоже возвращается в очередь,
    # а execute() интегрирует сохранённый патч без переисполнения
    rid_p = srv_runs.create("devrun", {"task": "с патчем"})
    srv_db.get_db().execute(
        "UPDATE runs SET status='running', patch='diff --git a/x b/x' WHERE id=?",
        (rid_p,))
    srv_db.get_db().commit()
    back_p = srv_devexec.requeue_interrupted()
    assert rid_p in back_p, back_p
    row_p = srv_db.get_db().execute("SELECT status, created_at FROM runs WHERE id=?",
                                    (rid_p,)).fetchone()
    assert row_p["status"] == "queued", dict(row_p)
    # свежая схема обязана содержать колонки, которые старые базы получили
    # ALTER'ом: их отсутствие в CREATE TABLE runs ломало devruns.finish на
    # чистой установке (миграции на пустой базе не выполняются — meta ещё нет)
    # recovery (01.11): done+патч без штампа 'integrated' — интеграция умерла на
    # рестарте (бой c2a639d2) — возвращается в очередь; со штампом — не трогается
    rid_r = srv_runs.create("devrun", {"task": "recovery"})
    srv_db.get_db().execute(
        "UPDATE runs SET status='done', patch='diff --git a/x b/x',"
        " result_text='готово', finished_at=datetime('now') WHERE id=?", (rid_r,))
    srv_db.get_db().commit()
    back2 = srv_devexec.requeue_interrupted()
    assert rid_r in back2, back2
    row_r = srv_db.get_db().execute("SELECT status FROM runs WHERE id=?", (rid_r,)).fetchone()
    assert row_r["status"] == "queued", dict(row_r)
    srv_db.get_db().execute(
        "UPDATE runs SET status='done', err='integrated abc123' WHERE id=?", (rid_r,))
    srv_db.get_db().commit()
    assert rid_r not in srv_devexec.requeue_interrupted(), "штамп integrated не защитил"
    cols_runs = {r[1] for r in srv_db.get_db().execute("PRAGMA table_info(runs)")}
    assert {"patch", "patch_hash"} <= cols_runs, sorted(cols_runs)
    print("8z. devexec: возврат оборванного + recovery done-с-патчем + схема runs OK")

    # 8aa (01.11): автопатч интегрирует патч на СВЕЖИЙ origin/main в одноразовом
    # worktree; устаревшая база → stale, боевое дерево цело (регрессия боя 01.11:
    # dc6b05 оставил unmerged-индекс и маркеры конфликта в боевом дереве — битый
    # клиент в раздаче, git блокировал любой коммит; deb3e8a/b7a38b3 на устаревшей
    # базе затёрли свежие правки main.py).
    origin = Path(TMP) / "int-origin.git"
    _gx(["init", "--bare", "-q", "-b", "main", str(origin)], Path(TMP))
    _gx(["remote", "add", "origin", str(origin)], src)
    _gx(["config", "user.email", "smoke@local"], src)
    _gx(["config", "user.name", "smoke"], src)
    _gx(GID + ["push", "-q", "origin", "main"], src)
    real_root2 = srv_ap.ROOT
    try:
        srv_ap.ROOT = src
        # (a) happy path: патч нового файла → интеграция → push → ff боевого дерева
        wt_a = Path(TMP) / "int-wt-a"
        _gx(["worktree", "add", "--detach", str(wt_a), "origin/main"], src)
        (wt_a / "c.txt").write_text("hello\n", encoding="utf-8")
        patch_a = srv_devexec.extract_patch(wt_a)
        _gx(["worktree", "remove", "--force", str(wt_a)], src)
        rid_a = srv_runs.create("devrun", {"task": "smoke: интеграция happy path"})
        ra = srv_ap.apply(rid_a, patch_a, "smoke интеграция", do_restart=False)
        assert ra.get("applied") and ra.get("committed") and ra.get("pushed"), ra
        assert (src / "c.txt").exists(), "боевое дерево не ff-нуто к origin/main"
        n_origin = int(_gx(["rev-list", "--count", "main"], origin))
        assert n_origin >= 2, n_origin
        # (b) устаревшая база: параллельная правка той же строки → stale,
        #     боевое дерево НЕ тронуто (нет ни маркеров, ни unmerged-индекса)
        wt_b = Path(TMP) / "int-wt-b"
        _gx(["worktree", "add", "--detach", str(wt_b), "origin/main"], src)
        (wt_b / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
        patch_b = srv_devexec.extract_patch(wt_b)
        _gx(["worktree", "remove", "--force", str(wt_b)], src)
        (src / "a.txt").write_text("one\nthree\n", encoding="utf-8")
        _gx(GID + ["commit", "-aqm", "параллельная правка"], src)
        _gx(GID + ["push", "-q", "origin", "main"], src)
        rid_b = srv_runs.create("devrun", {"task": "smoke: устаревшая база"})
        rb = srv_ap.apply(rid_b, patch_b, "", do_restart=False)
        assert rb.get("stale") is True and not rb.get("applied"), rb
        assert "устаревшая база" in (rb.get("error") or ""), rb
        assert (src / "a.txt").read_text(encoding="utf-8") == "one\nthree\n", \
            "боевое дерево повреждено при stale"
        assert "<<<<<<<" not in (src / "a.txt").read_text(encoding="utf-8")
        assert _gx(["ls-files", "-u"], src) == "", "остался unmerged-индекс"
        # keep.txt от 8y легитимно untracked — проверяем, что НЕ появилось нового:
        # ни правок a.txt, ни мусора apply
        st_b = _gx(["status", "--porcelain"], src)
        assert "a.txt" not in st_b and ".rej" not in st_b and ".orig" not in st_b, \
            f"боевое дерево грязное после stale: {st_b}"
    finally:
        srv_ap.ROOT = real_root2
        _gx(["worktree", "prune"], src)
    print("8aa. autopatch: интеграция на origin/main + stale-база не калечит дерево OK")

    # 8ab (мобильный доступ, образец Hermes Mobile): профили подключения телефона,
    # отзываемый токен профиля (после отзыва 401), mTLS-сертификат с отпечатком,
    # SSH-туннель с портом ИЗ КОНФИГА, QR и PIN-гейт мобильной сессии.
    import urllib.error

    def call(method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        r = urllib.request.Request(BASE + path, method=method, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                raw = resp.read().decode()
                return resp.status, raw, dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def call_json(method, path, body=None, headers=None):
        st, raw, _ = call(method, path, body, headers)
        try:
            return st, json.loads(raw)
        except Exception:
            return st, raw

    from server.config import load_config as _lc
    cfg_port = int(_lc().server.port)
    assert cfg_port == PORT, (cfg_port, PORT)

    st, prof = call_json("POST", "/v1/mobile/profiles",
                         {"name": "Смоук-телефон", "server_addr": "arman@mac.local",
                          "auth_mode": "bearer", "expires_in_days": 7})
    assert st == 201 and prof["id"] and prof["auth_mode"] == "bearer", (st, prof)
    assert "token" not in prof and not prof.get("has_pin"), prof
    pid = prof["id"]

    st, tok = call_json("POST", f"/v1/mobile/profiles/{pid}/token")
    assert st == 201 and tok["token"].startswith("nsm_"), (st, tok)
    assert len(tok["token_hint"]) == 4 and tok["warning"], tok
    assert f":{cfg_port}/" in tok["login_url"], tok          # порт из конфига
    token = tok["token"]

    st, lst = call_json("GET", "/v1/mobile/profiles")
    dump = json.dumps(lst, ensure_ascii=False)
    assert st == 200 and "nsm_" not in dump and token not in dump, "токен утёк в список"
    assert '"token"' not in dump and "pin_hash" not in dump, "секрет в карточке профиля"

    auth = {"Authorization": f"Bearer {token}"}
    st, _ = call_json("GET", "/v1/sessions", headers=auth)
    assert st == 200, ("валидный токен профиля должен работать как учётка", st)

    st, rev = call_json("POST", f"/v1/mobile/profiles/{pid}/revoke")
    assert st == 200 and rev["revoked"] is True, (st, rev)
    st, _ = call_json("GET", "/v1/sessions", headers=auth)
    assert st == 401, ("после отзыва токен обязан получить 401", st)
    st, _ = call_json("GET", "/v1/mobile/status", headers=auth)
    assert st == 401, st
    st, again = call_json("POST", f"/v1/mobile/profiles/{pid}/token")
    assert st == 409, ("отозванному профилю токен не выдаётся", st)
    print("8ab. mobile: выдача токена профиля + 401 после отзыва OK")

    # SSH-туннель: команда собирается с портом из конфига сервиса (не хардкод)
    st, tun_prof = call_json("POST", "/v1/mobile/profiles",
                             {"name": "Смоук-туннель", "server_addr": "owner@noosphere.local",
                              "auth_mode": "bearer", "expires_in_days": 0})
    assert st == 201, (st, tun_prof)
    tid = tun_prof["id"]
    st, tun = call_json("GET", f"/v1/mobile/profiles/{tid}/tunnel")
    assert st == 200, (st, tun)
    assert tun["command"] == f"ssh -N -L {PORT}:127.0.0.1:{PORT} owner@noosphere.local", tun
    assert tun["port"] == cfg_port and tun["port_source"] == "server.port", tun
    assert tun["loopback_url"] == f"http://127.0.0.1:{PORT}/", tun
    st, svg_cmd, hdrs = call("GET", f"/v1/mobile/profiles/{tid}/qr.svg?target=command")
    st2, svg_url, _ = call("GET", f"/v1/mobile/profiles/{tid}/qr.svg?target=url")
    assert st == 200 and svg_cmd.startswith("<svg") and svg_cmd.count("<rect") > 10, svg_cmd[:80]
    assert st2 == 200 and svg_url.startswith("<svg") and svg_cmd != svg_url, svg_url[:80]
    print("8ac. mobile: SSH-туннель с портом из конфига + QR команды и ссылки OK")

    # PIN: неверный — отказ без сессии, верный — сессия; без PIN инструменты закрыты
    st, pp = call_json("POST", "/v1/mobile/profiles",
                       {"name": "Смоук-PIN", "server_addr": "arman@mac.local",
                        "auth_mode": "bearer", "expires_in_days": 3, "pin": "2468"})
    assert st == 201 and pp["has_pin"] is True, (st, pp)
    ppid = pp["id"]
    st, ptok = call_json("POST", f"/v1/mobile/profiles/{ppid}/token")
    assert st == 201, (st, ptok)
    pauth = {"Authorization": f"Bearer {ptok['token']}"}

    st, bad = call_json("POST", "/v1/mobile/sessions",
                        {"token": ptok["token"], "pin": "0000"})
    assert st == 401, ("неверный PIN — отказ", st, bad)
    st, nopin = call_json("POST", "/v1/mobile/sessions", {"token": ptok["token"]})
    assert st == 401, ("сессия без PIN — отказ", st, nopin)
    st, ok = call_json("POST", "/v1/mobile/sessions",
                       {"token": ptok["token"], "pin": "2468", "client": "smoke-phone"})
    assert st == 200 and ok["pin_ok"] is True, (st, ok)
    msid = ok["session_id"]
    sauth = {**pauth, "X-Mobile-Session": msid}
    st, tools = call_json("GET", "/v1/mobile/tools", headers=sauth)
    assert st == 200 and tools["pin_ok"] is True, (st, tools)

    st, locked = call_json("POST", f"/v1/mobile/sessions/{msid}/lock")
    assert st == 200 and locked["pin_ok"] == 0, (st, locked)
    st, denied = call_json("GET", "/v1/mobile/tools", headers=sauth)
    assert st == 403, ("сессия без PIN не открывает инструменты", st, denied)
    st, _ = call_json("POST", f"/v1/mobile/sessions/{msid}/unlock", {"pin": "9999"})
    assert st == 401, ("неверный PIN разблокировки — отказ", st)
    st, unlocked = call_json("POST", f"/v1/mobile/sessions/{msid}/unlock", {"pin": "2468"})
    assert st == 200 and unlocked["pin_ok"] == 1, (st, unlocked)
    st, _ = call_json("GET", "/v1/mobile/tools", headers=sauth)
    assert st == 200, ("после верного PIN инструменты открыты", st)

    # экран состояния: сессии по профилям, последнее обращение, «завершить все»
    st, stat = call_json("GET", "/v1/mobile/status")
    assert st == 200 and stat["active_sessions"] >= 1, (st, stat)
    mine = next(p for p in stat["per_profile"] if p["id"] == ppid)
    assert mine["active_sessions"] == 1 and mine["last_request"], mine
    assert "nsm_" not in json.dumps(stat, ensure_ascii=False), "токен утёк в статус"
    st, closed = call_json("POST", "/v1/mobile/sessions/close-all")
    assert st == 200 and closed["closed"] >= 1, (st, closed)
    st, after = call_json("GET", "/v1/mobile/tools", headers=sauth)
    assert st == 403, ("закрытая сессия не открывает инструменты", st)
    assert call_json("GET", "/v1/mobile/status")[1]["active_sessions"] == 0
    print("8ad. mobile: PIN (неверный — отказ, верный — сессия) + статус/завершить все OK")

    # mTLS: сертификат генерирует сервер, отдаётся один раз, проверка по отпечатку
    st, mt = call_json("POST", "/v1/mobile/profiles",
                       {"name": "Смоук-mTLS", "server_addr": "arman@mac.local",
                        "auth_mode": "bearer", "expires_in_days": 5})
    assert st == 201, (st, mt)
    mid = mt["id"]
    st, cert = call_json("POST", f"/v1/mobile/profiles/{mid}/cert")
    assert st == 201, (st, cert)
    assert "BEGIN CERTIFICATE" in cert["cert_pem"] and "PRIVATE KEY" in cert["key_pem"], cert
    fp = cert["fingerprint_sha256"]
    assert len(fp) == 64 and cert["warning"], cert
    st, chk = call_json("POST", "/v1/mobile/mtls/check", {"fingerprint": fp})
    assert st == 200 and chk["profile_id"] == mid, (st, chk)
    st, prof_mt = call_json("GET", "/v1/mobile/profiles")
    card = next(p for p in prof_mt if p["id"] == mid)
    assert card["auth_mode"] == "mtls" and card["cert_fingerprint"] == fp, card
    assert "PRIVATE KEY" not in json.dumps(prof_mt), "ключ утёк в карточку профиля"
    call_json("POST", f"/v1/mobile/profiles/{mid}/revoke")
    st, _ = call_json("POST", "/v1/mobile/mtls/check", {"fingerprint": fp})
    assert st == 404, ("отозванный сертификат не проходит проверку", st)
    print("8ae. mobile: mTLS-сертификат, отпечаток в БД, отзыв гасит проверку OK")

    # 8af. правила v2: cost_p90 мерит расход ХОДА, а не одного вызова.
    # До v2 p90 брался по одному вызову, а spent суммировал все попытки хода
    # (~12 вызовов модели на ход), поэтому правило срабатывало почти всегда и
    # любой агентский ход становился «partial» при живых tech_ok/tool_ok.
    import sqlite3 as _sq
    from server import verdicts as _v
    _mem = _sq.connect(":memory:")
    _mem.row_factory = _sq.Row
    _mem.executescript("""
        CREATE TABLE session_turns(session_id TEXT, turn_no INTEGER, ts TEXT, snapshot TEXT);
        CREATE TABLE turns(session_id TEXT, ts TEXT, day TEXT, task_class TEXT, ok INTEGER,
                           answered INTEGER, finish TEXT, err TEXT, escalated INTEGER,
                           content_filter INTEGER, ms INTEGER, prompt_tokens INTEGER,
                           completion_tokens INTEGER, cost_usd REAL, tier_start TEXT,
                           tier_used TEXT, turn_last INTEGER, turn_prompt_tokens INTEGER,
                           turn_completion_tokens INTEGER);
        CREATE TABLE tool_audit(id INTEGER PRIMARY KEY, session_id TEXT, turn_no INTEGER,
                                tool TEXT, rc INTEGER, note TEXT, args_summary TEXT,
                                out_len INTEGER);
        CREATE TABLE approvals(session_id TEXT, ts TEXT, kind TEXT, status TEXT, summary TEXT);
    """)
    # история класса: 8 ходов с агрегатом ~20k токенов, каждый из 12 вызовов по 1.6k
    for _ in range(8):
        _mem.execute("INSERT INTO turns(day, task_class, ok, finish, turn_last,"
                     " turn_prompt_tokens, turn_completion_tokens, prompt_tokens,"
                     " completion_tokens) VALUES(date('now'),'chat',1,'stop',1,"
                     " 19000, 1000, 1500, 100)")
    # история route: классификатор дешёвый (p90 хода ~1100) — на него нельзя
    # мерить расход всего хода, если основная работа шла классом chat
    for _ in range(8):
        _mem.execute("INSERT INTO turns(day, task_class, ok, finish, turn_last,"
                     " turn_prompt_tokens, turn_completion_tokens, prompt_tokens,"
                     " completion_tokens) VALUES(date('now'),'route',1,'stop',1,"
                     " 1000, 100, 1000, 100)")

    def _turn(sid: str, prompt: int, completion: int, calls: int = 12) -> None:
        _mem.execute("INSERT INTO session_turns(session_id, turn_no, ts, snapshot)"
                     " VALUES(?,1,'2026-11-02T10:00:00Z','{}')", (sid,))
        for k in range(calls):
            _mem.execute("INSERT INTO turns(session_id, ts, day, task_class, ok, answered,"
                         " finish, err, escalated, content_filter, ms, prompt_tokens,"
                         " completion_tokens, cost_usd, tier_start, tier_used, turn_last,"
                         " turn_prompt_tokens, turn_completion_tokens)"
                         " VALUES(?,?,'2026-11-02','chat',1,1,'stop','',0,0,1000,?,?,0.0,"
                         " 'base','base',?,0,0)",
                         (sid, f"2026-11-02T10:00:{k:02d}Z", prompt, completion,
                          1 if k == calls - 1 else 0))

    _turn("A", 1500, 100)   # spent 19 200 ≤ p90 хода 20 000 → ход чистый
    _turn("B", 5000, 500)   # spent 66 000 > p90 хода → cost_p90 обязан сработать
    # ход C: первая попытка — классификатор route, остальные chat. Класс хода
    # обязан считаться по доминантному расходу, иначе cap берётся из
    # p90(route)=1100 и обычный чат с инструментами выглядит «дорогим».
    _mem.execute("INSERT INTO session_turns(session_id, turn_no, ts, snapshot)"
                 " VALUES('C',1,'2026-11-02T10:00:00Z','{}')")
    _mem.execute("INSERT INTO turns(session_id, ts, day, task_class, ok, answered,"
                 " finish, err, escalated, content_filter, ms, prompt_tokens,"
                 " completion_tokens, cost_usd, tier_start, tier_used, turn_last,"
                 " turn_prompt_tokens, turn_completion_tokens)"
                 " VALUES('C','2026-11-02T10:00:00Z','2026-11-02','route',1,1,'stop','',0,0,"
                 " 900,500,50,0.0,'base','base',0,0,0)")
    for k in range(11):
        _mem.execute("INSERT INTO turns(session_id, ts, day, task_class, ok, answered,"
                     " finish, err, escalated, content_filter, ms, prompt_tokens,"
                     " completion_tokens, cost_usd, tier_start, tier_used, turn_last,"
                     " turn_prompt_tokens, turn_completion_tokens)"
                     " VALUES('C',?,'2026-11-02','chat',1,1,'stop','',0,0,1000,1500,100,0.0,"
                     " 'base','base',0,0,0)", (f"2026-11-02T10:00:{k + 1:02d}Z",))
    _v.set_conn(_mem)
    try:
        _cap = _v._p90_turn_tokens(_mem, "chat")
        assert _cap == 20000, ("p90 хода считается по агрегату turn_last=1", _cap)
        _a = _v.eval_turn("A", 1, _mem)
        assert _a["tech_ok"] and _a["tool_ok"] and _a["cost_ok"], _a
        assert _a["verdict"] == "success", _a
        assert not [s for s in _a["signals"] if s["rule"] == "cost_p90"], _a["signals"]
        _b = _v.eval_turn("B", 1, _mem)
        assert _b["cost_ok"] is False and _b["verdict"] == "partial", _b
        _sig = next(s for s in _b["signals"] if s["rule"] == "cost_p90")
        assert "p90_turn(chat)=20000" in _sig["evidence"], _sig
        _cls = _v._turn_class(_v.turn_attempts(_mem, "C", 1))
        assert _cls == "chat", ("класс хода — доминантный по токенам, не первая попытка", _cls)
        _cc = _v.eval_turn("C", 1, _mem)
        assert _cc["cost_ok"] is True and _cc["verdict"] == "success", _cc
    finally:
        _v.set_conn(None)
        _mem.close()
    print("8af. verdicts v3: cost_p90 мерит ход и берёт класс по расходу OK")

    # 8ag. прогон: структурированный report вместо свободного текста и снятие
    # «done», если патч не дошёл до origin/main (до правки статус оставался
    # done при провалившемся smoke/грязном дереве — задача выглядела закрытой).
    from server import devruns as _dr, devexec as _dx, runs as _runs
    _rid = _dr.start("смоук-проверка отчёта прогона", source="chat:smoke")
    _dr.set_report(_rid, "progress")
    assert _dr.get_devrun(_rid)["report"] == "progress"
    for _bad in ("готово", "DONE", "", "waiting ", None):
        try:
            _dr.set_report(_rid, _bad)
        except ValueError:
            continue
        raise AssertionError(f"report вне enum принят: {_bad!r}")
    _runs.mark(_rid, "done", text="патч готов")
    _dx._mark_not_integrated(_rid, "смоук: интеграция не подтверждена")
    _row = _dr.get_devrun(_rid)
    assert _row["status"] == "error", _row
    assert "не дошёл до main" in (_row["err"] or ""), _row
    _dr.set_report(_rid, "failed")
    assert _dr.get_devrun(_rid)["report"] == "failed"
    _lst = {r["id"]: r for r in _dr.list_devruns(limit=50)}
    assert _lst.get(_rid, {}).get("report") == "failed", "report не виден в списке прогонов"
    print("8ag. devruns: report-enum + «done» снимается при неподтверждённой интеграции OK")

    # 8ah. бюджет статического префикса ПО КОНТЕКСТАМ: пустой чат-ход стоил
    # 4951 prompt-токен (замер 02.11 на бою, scripts/probe_prefix.py), а прежний
    # гейт считал только три куска — рост JSON инструментов, списка MCP, каталога
    # скилов и шапки памяти был невидим. Правила рабочего места и содержимое
    # памяти — данные владельца, в бюджет не входят.
    # (_pool нужен и ниже, в 8ak: цепочка роли судьи.)
    from server import pool as _pool, tools as _tools, skills as _skills, \
        memory as _memory, devexec as _devexec
    _specs_all = _tools.openai_specs()
    _specs_noshell = [s for s in _specs_all if s["function"]["name"]
                      not in ("shell_exec", "terminal_exec")]
    assert len(_specs_all) == 16, f"состав инструментов изменился: {len(_specs_all)}"
    assert not any(s["function"]["name"] == "vault_save" for s in _specs_all), \
        "vault_save не должен быть доступен модели (аудит r2, N-P1)"
    _mcp_fixed = len(_tools.MCP_HEADER) + len(_tools.MCP_TAIL)
    _prefix = {
        "chat_tools": (len(_tools.agent_system(allow_shell=True))
                       + len(_tools.preflight_block(_specs_all, allow_shell=True))
                       + _mcp_fixed + len(_memory.RECALL_HEADER)
                       + len(json.dumps(_specs_all, ensure_ascii=False))),
        "bg_no_shell": (len(_tools.agent_system(allow_shell=False))
                        + len(_tools.preflight_block(_specs_noshell, allow_shell=False))
                        + _mcp_fixed + len(_memory.RECALL_HEADER)
                        + len(json.dumps(_specs_noshell, ensure_ascii=False))),
        "devexec": (len(_devexec.EXEC_SYSTEM)
                    + len(json.dumps(_devexec._specs(), ensure_ascii=False))),
        "skills_1c": (len(_tools.agent_system(allow_shell=True))
                      + len(_tools.preflight_block(_specs_all, allow_shell=True))
                      + _mcp_fixed + len(_memory.RECALL_HEADER)
                      + len(_skills.worst_case_block())
                      + len(json.dumps(_specs_all, ensure_ascii=False))),
    }
    for _name, _chars in _prefix.items():
        assert _chars <= PREFIX_BUDGETS[_name], (
            f"префикс {_name} вырос до {_chars} знаков при бюджете "
            f"{PREFIX_BUDGETS[_name]} — режь текст, а не поднимай бюджет")
    print("8ah. бюджет префикса по контекстам OK: "
          + ", ".join(f"{k}={v}<={PREFIX_BUDGETS[k]}" for k, v in _prefix.items()))

    # 8ai. заметка в чат обязана называть ФАКТ: sha подтверждённого коммита.
    # Бой 02.11 (прогон 95be9f34a99a): devexec не передавал commit_sha, и
    # заметка говорила «патч уже был в origin/main» про свежий коммит 33961ed.
    import asyncio as _aio
    from server import main as _m
    _rid2 = _dr.start("смоук-текст заметки", source=f"chat:{sid}")
    for _rep, _must, _mustnot in (
        ({"ok": True, "verified": True, "commit_sha": "abc1234def90",
          "committed": True, "pushed": True, "restart": True,
          "files": ["README.md"]}, "abc1234def90 подтверждён в origin/main", "уже был"),
        ({"ok": True, "verified": True, "already": True, "skip_push": True,
          "files": []}, "патч уже был в origin/main", "подтверждён"),
    ):
        _aio.run(_m._notify_devrun(_rid2, "смоук-текст заметки", dict(_rep)))
        from server.db import get_db as _smk_db
        _snap = _smk_db().execute(
            "SELECT snapshot FROM session_turns WHERE session_id=? ORDER BY turn_no DESC"
            " LIMIT 1", (sid,)).fetchone()
        _txt = json.loads(_snap["snapshot"] or "{}").get("text", "")
        assert _must in _txt, (_must, _txt)
        assert _mustnot not in _txt, (_mustnot, _txt)
    print("8ai. заметка прогона называет sha подтверждённого коммита OK")

    # 8aj. egress-failover: отказ маршрута (privoxy 503 / connect) не означает
    # отказ провайдера — тот же запрос повторяется другим транспортом.
    # Бой 01.11: 12 × ProxyError('503 Forwarding failure') за 7 минут оставили
    # ночного судью с одним proxy-вердиктом на всю ночь (туннель egress сделал
    # 4611 рестартов за сутки); 30.10 — наоборот, 24 × ConnectError('') прямо.
    import httpx as _httpx
    from server import pool as _p2
    _routes: list[str] = []
    _mode = "proxy_dead"

    class _FakeResp:
        status_code = 200
        text = '{"choices": []}'

        def json(self):
            return {"choices": []}

    class _Fake429:
        status_code = 429
        text = "rate limit"

        def json(self):
            return {}

    class _FakeClient:
        def __init__(self, via: str):
            self.via = via

        async def post(self, url, json=None, headers=None, timeout=None):
            _routes.append(self.via or "direct")
            if _mode == "both_dead":
                raise _httpx.ConnectError("")
            if _mode == "proxy_dead" and self.via:
                raise _httpx.ProxyError("503 Forwarding failure")
            if _mode == "rate_limit":
                return _Fake429()
            return _FakeResp()

    _real_client_for = _p2._client_for
    _p2._client_for = lambda via: _FakeClient(via)
    try:
        _st, _resp = asyncio.run(_p2._post("http://x/chat", {},
                                           proxy="http://p:18118", alt_proxy=""))
        assert _st == 200, (_st, _resp)
        assert _routes == ["http://p:18118", "direct"], _routes
        # ответ провайдера (429) маршрут НЕ меняет: повтор по другому транспорту
        # просто оплатил бы тот же отказ дважды
        _routes.clear()
        _mode = "rate_limit"
        _st, _resp = asyncio.run(_p2._post("http://x/chat", {},
                                           proxy="http://p:18118", alt_proxy=""))
        assert _st == 429 and _routes == ["http://p:18118"], (_st, _routes)
        # мертвы оба маршрута — ошибка называет оба, а не только запасной
        _routes.clear()
        _mode = "both_dead"
        _st, _resp = asyncio.run(_p2._post("http://x/chat", {},
                                           proxy="http://p:18118", alt_proxy=""))
        assert _st == 0, (_st, _resp)
        assert _resp.count("ConnectError") == 2 and "запасной маршрут" in _resp, _resp
        assert _routes == ["http://p:18118", "direct"], _routes
        # без alt_proxy поведение прежнее: один маршрут, одна ошибка
        _routes.clear()
        _mode = "proxy_dead"
        _st, _resp = asyncio.run(_p2._post("http://x/chat", {}, proxy="http://p:18118"))
        assert _st == 0 and _routes == ["http://p:18118"], (_st, _routes)
        assert "ProxyError" in _resp, _resp
    finally:
        _p2._client_for = _real_client_for
    print("8aj. egress-failover: мёртвый прокси не роняет вызов, 429 маршрут не меняет OK")

    # 8ak. роль → модель: настроенная роль задаёт маршрут явно и идёт по СВОИМ
    # запасным, а не по лестнице тиров (base и mid — одна модель, 95% объёма) и
    # не падает на глобальный fallback_tier, который для судьи означает вердикт
    # бесплатной флеш-модели, неотличимый в отчёте от настоящего.
    from server import router as _rt
    assert _rt.role_chain("judge") == [
        {"channel": "zen", "model": "role-judge-a", "body": {}, "max_tokens": 0},
        {"channel": "alibaba", "model": "role-judge-b", "body": {}, "max_tokens": 0},
    ], _rt.role_chain("judge")
    assert _rt.role_chain("worker") == [], "ненастроенная роль обязана оставить лестницу"
    assert _rt.role_chain("") == []
    _sid_role = "smoke-role-chain"
    _rr = asyncio.run(_pool.ask("audit", "судейский запрос", role="judge",
                                force_no_escalate=True, check_json=True,
                                session_id=_sid_role, user="smoke", turn_kind="judge"))
    assert _rr["ok"] is False, _rr
    _seen = [t.get("model") for t in _rr["trace"] if t.get("model")]
    assert "role-judge-a" in _seen and "role-judge-b" in _seen, _rr["trace"]
    assert any("запасной маршрут" in str(t.get("why") or "") for t in _rr["trace"]), _rr["trace"]
    assert not _rr.get("fallback"), "в цепочке роли глобальный fallback_tier не применяется"
    from server.db import get_db as _rdb
    _rows = _rdb().execute(
        "SELECT channel, model, escalated FROM turns WHERE session_id=? ORDER BY id",
        (_sid_role,)).fetchall()
    _chans = [x["channel"] for x in _rows]
    assert _chans and set(_chans) == {"zen", "alibaba"}, _chans
    assert any(x["escalated"] for x in _rows), "смена маршрута роли видна как escalated"
    # явный tier= остаётся меткой тира в телеметрии, но модель даёт роль
    _rr2 = asyncio.run(_pool.ask("code", "задача исполнителя", tier="deep",
                                 role="judge", session_id="smoke-role-tier",
                                 user="smoke"))
    assert _rr2["tier"] == "deep", _rr2
    assert _rr2["trace"][-1]["model"] == "role-judge-b", _rr2["trace"]
    print("8ak. роль → модель: своя цепочка запасных, без лестницы и без fallback_tier OK")

    # 8al. Приёмка владельцем: золотая метка на чат перевешивает правила, уходит
    # судье уликой и возвращает чат на пересуд. До 02.11 owner_marks не писал
    # никто кроме selftest (0 строк за всё время), кнопки в окне не было, и судья
    # ставил partial за неподтверждённую приёмку — 21 partial против 29 success за
    # 30 дней: разрыв между «сделано» и «принято» нечем было закрыть.
    from server import chat_judge as _cj, transcript as _tr

    def _broken_chat(tag: str) -> str:
        """Чат с одним ходом и упавшей попыткой: правила обязаны сказать fail."""
        s = srv_sessions.create_session("arman", "test", "smoke")
        t = srv_sessions.append_turn(s, {"prompt": f"почини {tag}"})
        srv_sessions.complete_turn(s, t, 0, {"prompt": f"почини {tag}",
                                             "text": f"починил {tag}", "ok": True})
        ts = dbx.execute("SELECT ts FROM session_turns WHERE session_id=? AND turn_no=1",
                         (s,)).fetchone()["ts"]
        dbx.execute(
            "INSERT INTO turns(session_id, ts, day, task_class, ok, answered, finish,"
            " err, escalated, content_filter, ms, prompt_tokens, completion_tokens,"
            " cost_usd, tier_start, tier_used, turn_last, turn_kind)"
            " VALUES(?,?,'2026-11-02','chat',0,0,'stop','boom',0,0,100,10,5,0.0,"
            " 'base','base',1,'inline')", (s, ts))
        dbx.commit()
        _v.recompute_session(s)
        return s

    _sidm = _broken_chat("метку")
    assert _v.chat_proxy_verdict(_sidm)[0] == "fail", "до метки правила обязаны дать fail"
    st, mk = req("POST", f"/v1/sessions/{_sidm}/mark", {"mark": "ok"})
    assert st == 200 and mk["effective"] == "ok" and mk["status"] == "success", mk
    assert mk["stale"] is False and mk["rejudge"] == "scheduled", mk
    px, sg = _v.chat_proxy_verdict(_sidm)
    assert px == "success", ("принятый владельцем чат не остаётся fail", px)
    assert any(s["rule"] == "owner_mark" for s in sg), sg
    st, lst = req("GET", "/v1/sessions")
    assert next(s for s in lst if s["id"] == _sidm)["owner_mark"] == "ok", \
        "действующая метка не видна в списке сессий"
    st, one = req("GET", f"/v1/sessions/{_sidm}")
    assert one["owner_mark"] == "ok" and one["owner_mark_ts"], one.get("owner_mark")
    st, mk = req("POST", f"/v1/sessions/{_sidm}/mark", {"mark": "notit", "note": "не починилось"})
    assert st == 200 and mk["status"] == "fail", mk
    st, mk = req("POST", f"/v1/sessions/{_sidm}/mark", {"mark": "none"})
    assert st == 200 and mk["effective"] == "" and mk["status"] == "", mk
    assert _v.chat_proxy_verdict(_sidm)[0] == "fail", "снятая метка возвращает чат правилам"
    st, _ = req_status("POST", f"/v1/sessions/{_sidm}/mark", {"mark": "maybe"})
    assert st == 400, st
    st, _ = req_status("POST", "/v1/sessions/nesuschestvuyuschego/mark", {"mark": "ok"})
    assert st == 404, st

    # метка в транскрипте судьи и в промпте; вердикт судьи перебивается детерминированно
    _v.owner_mark(_sidm, "notit", "сделало не то")
    txt, stats = _tr.condensed(_sidm)
    assert "[метки владельца]" in txt and "не то" in txt, txt[-300:]
    assert stats["owner_mark"] == "notit" and stats["owner_effect"]["status"] == "fail", stats
    assert "метка владельца" in _cj.build_prompt(txt, stats), "промт судьи не несёт метку"
    assert "метка владельца" not in _cj.build_prompt(
        txt, {"turns": 1, "attempts": 1, "tools": 0}), "аддендум липнет к чату без метки"
    pj = {"status": "partial", "reason": "приёмка не подтверждена", "soft_status": "partial",
          "soft_reason": "польза была", "evidence_turns": [1], "confidence": 0.7}
    cl = _cj._clamp_by_mark(pj, stats)
    assert cl["status"] == "fail" and cl["soft_status"] == "fail", cl
    assert "не то" in cl["reason"] and "приёмка не подтверждена" in cl["reason"], cl
    assert _cj._clamp_by_mark(pj, {"owner_effect": {"mark": "", "status": ""}}) == pj, \
        "без метки вердикт судьи не меняется"

    # судья сам сказал то же, что владелец: его текст не переписывается, но мягкий
    # статус приводится к метке (бой 02.11: обе живые метки «не то» легли на
    # hard=fail/soft=partial, и отчёт продолжил считать закрытый чат вариативным)
    pj_hard = {"status": "fail", "reason": "судья сам сказал fail",
               "soft_status": "partial", "soft_reason": "прогресс был",
               "evidence_turns": [1], "confidence": 0.6}
    cl2 = _cj._clamp_by_mark(pj_hard, stats)
    assert cl2["status"] == "fail" and cl2["soft_status"] == "fail", cl2
    assert cl2["reason"] == pj_hard["reason"], "текст судьи переписан при совпавшем статусе"
    assert "метке владельца" in cl2["soft_reason"], cl2["soft_reason"]
    _aligned = {**pj_hard, "soft_status": "fail"}
    assert _cj._clamp_by_mark(_aligned, stats) == _aligned, \
        "вердикт, который уже совпал с меткой, менять не за чем"

    # устаревшая метка (после неё был ход) золото теряет: остаётся уликой, а не
    # оправданием — иначе вчерашнее «принято» закрывало бы сегодняшний развал
    later = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=30)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    dbx.execute("INSERT INTO session_turns(session_id, turn_no, ts, status, snapshot,"
                " tokens_spent) VALUES(?,2,?,'done','{}',0)", (_sidm, later))
    dbx.commit()
    _v.recompute_session(_sidm)
    eff = _v.owner_effect(_sidm)
    assert eff["stale"] is True and eff["status"] == "", eff
    assert _v.chat_proxy_verdict(_sidm)[0] == "fail", "устаревшая метка не оправдывает чат"

    # пересуд: вердикт, вынесенный до метки, больше не действителен
    _sidn = srv_sessions.create_session("arman", "test", "smoke")
    dbx.execute("INSERT OR REPLACE INTO chat_verdict(session_id, status, reason, evidence,"
                " confidence, judge_model, judge_tier, tokens, rules_agree,"
                " judge_prompt_version, rules_proxy, mode, ts, closed_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_sidn, "success", "старый вердикт", "[]", 0.9, "k3", "deep", 120, 1, 2,
                 "success", "nightly", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    dbx.commit()
    assert _cj._needs_judge(_sidn) is False, "без метки и без новых ходов пересуд не нужен"
    _v.owner_mark(_sidn, "ok", "принял")
    assert _cj._needs_judge(_sidn) is True, "метка новее вердикта обязана вернуть чат судье"

    # сквозной: метка → фоновый пересуд → вердикт (судья в smoke мёртв → proxy правил,
    # который метку тоже учитывает: даже без LLM статус становится честным)
    _sidj = _broken_chat("пересуд")
    st, mk = req("POST", f"/v1/sessions/{_sidj}/mark", {"mark": "ok"})
    assert st == 200 and mk["status"] == "success", mk
    vrow = None
    for _ in range(60):
        vrow = dbx.execute("SELECT status, reason, rules_proxy, mode FROM chat_verdict"
                           " WHERE session_id=?", (_sidj,)).fetchone()
        if vrow:
            break
        time.sleep(0.5)
    assert vrow is not None, "пересуд после метки не записал вердикт"
    assert vrow["status"] == "success" and vrow["rules_proxy"] == "success", dict(vrow)
    assert (vrow["reason"] or "").startswith("судья недоступен"), dict(vrow)
    assert vrow["mode"] == "mark", f"пересуд после метки не отличим от ночного: {dict(vrow)}"
    print("8al. приёмка владельцем: метка перевешивает правила, уходит судье, пересуд OK")

    # 8am. Выдуманные моделью имена инструментов — алиасы, а не отказ.
    # Замер 03.11 (tool_audit, 30 дней): vault_rest 32 вызова, shell_command 15 и
    # ещё 4 с битым именем 'shell_command" string="true' — все с note='refuse'.
    # Префлайт-текст «vault_rest не существует» (cde8631) их не вылечил. Отказ
    # возвращался модели как обычный tool-результат, но раунд агентского цикла
    # при этом сгорал целиком: каждый раунд переотправляет модели весь контекст
    # хода (рост ~983 токена на раунд, p50 первого вызова 4 243).
    _ref_before = dbx.execute(
        "SELECT COUNT(*) c FROM tool_audit WHERE note='refuse'").fetchone()["c"]
    _al1 = _aio.run(_tools.execute_tool_call(
        {"id": "al1", "function": {"name": "vault_rest",
                                   "arguments": json.dumps({"query": "smoke-alias-none"})}}))
    assert not _al1.startswith("неизвестный инструмент"), _al1
    _al2 = _aio.run(_tools.execute_tool_call(
        {"id": "al2", "function": {"name": "shell_command",
                                   "arguments": json.dumps({"command": "echo smoke-alias-ok"})}},
        ctx={"allow_shell": True}))
    assert "smoke-alias-ok" in _al2 and not _al2.startswith("rc="), _al2
    _ref_after = dbx.execute(
        "SELECT COUNT(*) c FROM tool_audit WHERE note='refuse'").fetchone()["c"]
    assert _ref_after == _ref_before, f"алиас дал отказ: {_ref_before} -> {_ref_after}"
    _last2 = [r["tool"] for r in dbx.execute(
        "SELECT tool FROM tool_audit ORDER BY id DESC LIMIT 2").fetchall()]
    assert set(_last2) == {"vault_search", "shell_exec"}, \
        f"аудит обязан писать каноническое имя, а не выдуманное: {_last2}"
    assert dbx.execute("SELECT COUNT(*) c FROM tool_audit WHERE tool='shell_exec'"
                       " ORDER BY id DESC LIMIT 1").fetchone()["c"] >= 1
    _al_rc = dbx.execute("SELECT rc FROM tool_audit WHERE tool='shell_exec'"
                         " ORDER BY id DESC LIMIT 1").fetchone()["rc"]
    assert _al_rc == 0, f"rc алиаса не определён (канонизация имени не сработала): {_al_rc}"
    # настоящее неизвестное имя — по-прежнему отказ, и алиасы в подсказке не светятся
    _al3 = _aio.run(_tools.execute_tool_call(
        {"id": "al3", "function": {"name": "vault_restore", "arguments": "{}"}}))
    assert _al3.startswith("неизвестный инструмент"), _al3
    _avail = _al3.split("доступны ", 1)[1].split(", ")
    assert "vault_rest" not in _avail and "shell_command" not in _avail, _avail
    assert "vault_save" not in _avail, f"подсказка рекламирует недоступный инструмент: {_avail}"
    assert len(_avail) == 16, _avail
    assert len(_tools.openai_specs()) == 16, "алиасы не должны менять состав инструментов"
    assert not [s for s in _tools.openai_specs()
                if s["function"]["name"] in _tools._TOOL_ALIASES], "алиас виден модели"
    print("8am. алиасы выдуманных имён: vault_rest→vault_search, shell_command→shell_exec OK")

    # 8an. Компакция истории обязана быть ДОСТИЖИМОЙ. Единственный триггер
    # («70% окна» = 91 750 токенов на станции 131 072) лежал выше максимума
    # контекста чат-хода за 7 дней (77 134, p90 29 192), поэтому на чат-ходах
    # компакция не срабатывала ни разу, а расход хода квадратичен по числу
    # раундов: 10 ходов с ≥40 раундами = 44,5% расхода чата (19,7M из 44,4M).
    # Ужатие отдельных сообщений (_shrink_tool_history) для этого не годится:
    # оно сбрасывает префикс-кэш (91,5% prompt-токенов чата — cache_read).
    assert _pool._compact_above_tokens() > 0, "порог компакции по контексту выключен"
    assert _pool.COMPACT_MAX >= 4, _pool.COMPACT_MAX
    _seen: list[tuple] = []
    _round = {"n": 0}

    async def _fake_call(channel_name, tier, messages, max_tokens=1024, system=None,
                         spec=None, tools=None, timeout=None):
        _round["n"] += 1
        n = _round["n"]
        pt = 30_000 if n <= 2 else 5_000
        _seen.append((n, tools is None, len(messages), pt))
        usage = {"prompt_tokens": pt, "completion_tokens": 30,
                 "prompt_tokens_details": {"cached_tokens": 0}}
        if tools is None:                     # вызов компакции / wrap-up
            return {"ok": True, "text": "ЗАДАЧА: смоук. ФАКТЫ: контекст сжат.",
                    "model": "fake", "usage": usage, "finish": "stop", "err": "",
                    "tier": tier}
        if n >= 5:                            # ход завершается текстом
            return {"ok": True, "text": "готово", "model": "fake", "usage": usage,
                    "finish": "stop", "err": "", "tier": tier}
        _tc = [{"id": f"c{n}", "type": "function",
                "function": {"name": "shell_exec", "arguments": '{"command": "true"}'}}]
        return {"ok": True, "text": "", "model": "fake", "usage": usage,
                "finish": "tool_calls", "err": "", "tier": tier, "tool_calls": _tc,
                "assistant_message": {"role": "assistant", "content": "",
                                      "tool_calls": _tc}}

    async def _fake_exec(_tc):
        return "ok"

    _specs_c = [{"type": "function", "function": {"name": "shell_exec"}}]
    _real_call = _pool.call
    _real_thr = _pool._compact_above_tokens

    def _run_compact(sid: str) -> dict:
        return _aio.run(_pool.ask("chat", "смоук компакции", force_channel="zen",
                                  force_spec={"model": "fake", "body": {}},
                                  tools=_specs_c, tool_executor=_fake_exec,
                                  max_tool_iters=30, turn_budget=10_000_000,
                                  session_id=sid, user="smoke", turn_kind="inline"))

    _pool.call = _fake_call
    try:
        _sid_c = "smoke-compact"
        _rc = _run_compact(_sid_c)
    finally:
        _pool.call = _real_call
    assert _rc["ok"] is True, _rc
    assert any(str(t.get("why", "")).startswith("компакция #1") for t in _rc["trace"]), \
        _rc["trace"]
    _comp = [i for i, s in enumerate(_seen) if s[1]]
    assert _comp, f"вызова компакции не было: {_seen}"
    # после компакции история заменена на [задача, состояние, «продолжай»] — 3
    # сообщения, и рост контекста начинается заново, а не продолжается с прежнего
    assert _seen[_comp[0] + 1][1] is False and _seen[_comp[0] + 1][2] == 3, _seen
    assert _seen[_comp[0] + 2][2] > _seen[_comp[0] + 1][2], _seen
    assert dbx.execute("SELECT COUNT(*) c FROM turns WHERE session_id=? AND why='compact #1'",
                       (_sid_c,)).fetchone()["c"] >= 1, "компакция не видна в телеметрии"
    # порог — конфиг: 0 выключает компакцию по контексту, ход доигрывается как раньше
    _round["n"] = 0
    _seen.clear()
    _pool._compact_above_tokens = lambda: 0
    _pool.call = _fake_call
    try:
        _rc2 = _run_compact("smoke-compact-off")
    finally:
        _pool.call = _real_call
        _pool._compact_above_tokens = _real_thr
    assert _rc2["ok"] is True, _rc2
    assert not [s for s in _seen if s[1]], f"с порогом 0 компакции быть не должно: {_seen}"
    assert max(s[2] for s in _seen) >= 7, f"без компакции история обязана расти: {_seen}"
    print("8an. компакция истории: абсолютный порог срабатывает, tools.compact_above_tokens=0"
          " выключает OK")

    server.should_exit = True
    t.join(timeout=10)

    # телеметрия хода записана даже при провале каналов
    from server.db import get_db
    rows = get_db().execute("SELECT COUNT(*) c FROM turns").fetchone()
    assert rows["c"] >= 1, "telemetry пуста"
    print("9. telemetry OK, turns:", rows["c"])
    print("SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
