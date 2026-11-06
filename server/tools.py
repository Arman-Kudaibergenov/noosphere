"""tools.py — встроенные инструменты агента (P0-5б, FOUNDING §16.3 батч 2).

Инструменты первого набора: shell_exec, terminal_exec, read_file, write_file,
web_fetch, memory_save/memory_search, vault_*. Сервер однопользовательский
(Bearer-токен владельца), но предохранители обязательны (аудит r2/r3): таймаут
команды, потолок вывода/записи, джейл путей, deny-list команд, shell-guard
путей, SSRF-фильтр, журнал исполнения tool_audit
(args_summary без секретов, turn_no, rc).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
from pathlib import Path

import httpx

from . import memory, terminals, vault
from .config import load_config
from .db import get_db, now_iso

# Системный блок агентского хода собирается из частей по контексту (резка 02.11):
# ходу без shell не нужен текст про терминал, исполнителю прогонов (у него свой
# EXEC_SYSTEM) — блок про доработку Ноосферы. Замер scripts/measure_prefix.py на
# бою, дерево 253ec19: пустой чат-ход с инструментами стоил 4951 prompt-токен,
# префикс chat_tools — 14 838 знаков, из них этот блок 3448 и JSON инструментов
# 6558. Правила рабочего места (rules.compose, 2188 зн) и содержимое памяти —
# данные владельца, в резку не входят.
AGENT_CORE = (
    "Работай ДО РЕЗУЛЬТАТА: короткий план → разведка → действие → проверка. Просит "
    "владелец — делай в этом же ходе, без «сделать?», «подтверди» и без подмены "
    "действия планом. Нужен факт с машины или из сети — добудь инструментом, а не "
    "отвечай «я не могу»; нет специального инструмента — бери терминал. Разведка "
    "точечная: конкретный вопрос конкретному инструменту, а не выгрузка памяти "
    "пачками; известное не перечитывай, проверяемое сам — не спрашивай. "
    "Ход ограничен временем и токенами, а не числом шагов: бюджет кончился раньше "
    "результата — инструменты НЕ пропали, скажи прямо, что сделано, что осталось и с "
    "какого действия продолжить. Перед удалением или перезаписью данных покажи, что "
    "сделаешь. «Сделано/сохранено/запущено» — только по факту вызова инструмента в "
    "этом ходе; id — из его ответа. Ответ — после инструментов, коротко и по делу."
)

# Доступы и память — отдельной частью: они нужны и ходу без shell.
AGENT_DATA = (
    "Доступы контура (ssh, MCP, токены, логины) — в vault: не гадай, ищи "
    "vault_search; секрет — vault_get(reveal=true), только когда нужен для действия. "
    "Факты о контуре — memory_search/memory_save; сервисы — mcp_tools/mcp_call."
)

AGENT_DEVRUN = (
    "Доработку самой Ноосферы (код, окно, интерфейс, метрики) ставь devrun_request "
    "сразу и без вопроса, вместо «у меня нет инструментов»: согласование владельца "
    "не требуется — не обещай его и не жди. Постановка прогона — «в работе», а НЕ "
    "«сделано»: «задача закрыта» — только после системной заметки сервера о "
    "подтверждении патча в origin/main, до неё не выдавай план за результат. Прогоны "
    "живут ТОЛЬКО в таблице runs: проверяй их run_list, а не памятью; «запущен» — "
    "только получив run_id. MCP run_orchestrator — чужой реестр: свои прогоны там не "
    "запускай и не проверяй."
)

AGENT_SHELL = (
    "Для серий команд и CLI — terminal_exec (постоянный PTY): процесс идёт — poll за "
    "выводом, input в stdin, close для остановки; shell_exec — одна команда без "
    "состояния."
)


def agent_system(*, allow_shell: bool = True, devrun: bool = True) -> str:
    """Системный блок агентского хода под контекст. Полный блок — AGENT_SYSTEM."""
    parts = [AGENT_CORE, AGENT_DATA]
    if devrun:
        parts.append(AGENT_DEVRUN)
    if allow_shell:
        parts.append(AGENT_SHELL)
    return "\n".join(parts)


# полный блок: под-агент delegate и прежние ссылки на константу
AGENT_SYSTEM = agent_system()


def preflight_block(specs: list, *, allow_shell: bool) -> str:
    """Префлайт состава хода (порт из канона 29.10.2026): модель заранее видит, чего
    в ходе нет и где границы инструментов, а не узнаёт отказом посреди задачи.

    Границы джейла/deny объявлены ЗДЕСЬ, а не в AGENT_SYSTEM: в чат-ход system
    собирается из rules.compose() (непусто), поэтому `if not system: AGENT_SYSTEM`
    не срабатывает и агентский блок до модели не доходит (проверено 2026-11-01).
    preflight_block кладётся в prompt_llm и доходит всегда. Без объявления агент
    бился о джейл вслепую и жёг итерации на отказах (разбор tool_audit 01.11:
    ~86 отказов за 3 дня — write_file в ~/noosphere и ~/noosphere-android, чтение
    ~/noosphere/server, пробы crontab/launchctl/~/.ssh)."""
    if not allow_shell:
        return ("[Состав этого хода: shell_exec/terminal_exec недоступны (ход фоновый). "
                "Имя инструмента — ровно из списка; чего в списке нет, вызывать нельзя: "
                "не выдумывай вызов и не заявляй действие, не выполненное инструментом, "
                "— скажи прямо, чего не хватает.]")
    return ("[Границы инструментов — нарушишь и потеряешь итерацию на отказ: имя ровно "
            "из списка; запись только в ~/workspace и /tmp; ~/noosphere (код, config.yaml, "
            "БД) не читается вовсе — доработку Ноосферы ставь devrun_request; ~/.ssh, "
            "~/.secrets, ~/Library, ~/bin закрыты; запрещены sudo, crontab, launchctl, "
            "rm -rf /, shutdown/reboot.]")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")

# Джейл путей (аудит r2, N-P0-1 п.3; ужесточение r3 G1.1-2): секреты —
# ТОЛЬКО через vault. Отказ подделать нельзя: путь приводится к resolved.
# Запись: ~/workspace и /tmp — ВСЁ. Дерево деплоя (~/noosphere: код, БД с
# vault.secret) и леджер приёмки (~/bc-personal-data) на запись НЕ достижимы
# исполняющим субъектом; ~/noosphere/data туда же не добавляем — БД недостижима.
_JAIL_ALLOW_WRITE = ("~/workspace", tempfile.gettempdir(), "/tmp")
# Чтение: allowlist вместо фолбэка «весь $HOME» (r3 b) + tools.read_allow из
# конфига. ~/noosphere НЕ читается вовсе — ни код, ни config.yaml с токеном,
# ни data/noosphere.db.
_JAIL_ALLOW_READ = ("~/workspace", tempfile.gettempdir(), "/tmp")
_JAIL_DENY = ("~/.ssh", "~/.secrets", "~/.git-credentials", "~/bin", "~/Library",
              "~/.kimi-code", "~/.netrc", "~/.aws", "~/.docker", "~/.config",
              "~/noosphere")


def _jail_allow_read() -> tuple:
    extra = tuple(load_config().tools.read_allow or ())
    return _JAIL_ALLOW_READ + extra


def _jail_ok(path: Path, *, write: bool = False) -> bool:
    p = path.expanduser().resolve()
    for d in _JAIL_DENY:
        dd = Path(d).expanduser().resolve()
        if p == dd or p.is_relative_to(dd):
            return False
    allow = _JAIL_ALLOW_WRITE if write else _jail_allow_read()
    for a in allow:
        aa = Path(a).expanduser().resolve()
        if p == aa or p.is_relative_to(aa):
            return True
    return False


# Deny-list команд (аудит r2, N-P0-1 п.4). Сознательное отклонение от списка
# аудита: ssh и git push НЕ блокируем намертво — ssh = основной транспорт
# контура (доступы лежат в vault именно для этого), git push нужен потоку
# разработки; механизм заявок A3 убран решением владельца (2026-10-17) —
# ssh и git push исполняются напрямую, deny-list выше не тронут.
_CMD_DENY = (
    re.compile(r"\bsudo\b"),
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+/(\s|$)"),   # rm -rf /
    re.compile(r"\brm\s+-[a-zA-Z]*\s+~?/(\s|$)"),                     # rm … /
    re.compile(r"\bcrontab\b"),
    re.compile(r"\blaunchctl\b"),
    re.compile(r"\bmkfs\b|\bdd\s+.*of=/dev/|\bshutdown\b|\breboot\b"),
    re.compile(r"(curl|wget)[^|]*\|\s*(sudo\s+)?(ba)?sh\b"),          # curl … | sh
    re.compile(r":\s*\(\s*\)\s*\{"),                                  # fork-бомба
)


def _cmd_denied(command: str) -> str | None:
    for rx in _CMD_DENY:
        if rx.search(command):
            return rx.pattern
    return None


# Shell-guard путей (_PATH_GUARD_DENY) и исполнение скриптов из каталогов записи
# (_SCRIPT_EXEC_DENY) СНЯТЫ решением владельца 2026-10-30. Причина: эвристика
# обходилась индирекцией (что было задокументировано), а стоила дороже защиты —
# 51 отказ за 29.10, ~половина ложные на УДАЛЁННЫХ путях внутри ssh-команд
# (ssh hermes 'ls ~/.config'), вклад в настоящие 39,5% fail и в сожжённые бюджеты
# обходов (cost_p90 spent=346k при p90(chat)=16k). Модель угроз — решение 17.10:
# ssh/git push исполняются напрямую, заявок нет, агент работает с правами
# владельца. Настоящая защита осталась: джейл файловых инструментов (_jail_ok —
# секреты, config.yaml, noosphere.db и боевое дерево недостижимы) и _CMD_DENY
# (sudo, rm -rf /, crontab, launchctl, mkfs/dd, shutdown/reboot, curl|sh,
# fork-бомба). Не возвращать path-guard без нового решения владельца.


def openai_specs() -> list[dict]:
    """Описания инструментов в формате OpenAI tools[]."""
    specs = [
        {"type": "function", "function": {
            "name": "shell_exec",
            "description": "Shell-команда на сервере (macOS) без состояния; возвращает stdout+stderr.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": "сек, дефолт 30"},
            }, "required": ["command"]}}},
        {"type": "function", "function": {
            "name": "terminal_exec",
            "description": "Постоянный PTY-терминал macOS: CLI, cd/export, pipe; input — ввод, poll — вывод, close — стоп.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string"},
                "input": {"type": "string", "description": "stdin; спецклавиши — JSON control chars"},
                "enter": {"type": "boolean"},
                "poll": {"type": "boolean"},
                "close": {"type": "boolean"},
                "timeout": {"type": "integer"},
                "terminal_id": {"type": "string"},
            }}}},
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Прочитать текстовый файл с сервера.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "max_chars": {"type": "integer"},
            }, "required": ["path"]}}},
        {"type": "function", "function": {
            "name": "write_file",
            "description": "Записать текст в файл (создаёт родительские каталоги).",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            }, "required": ["path", "content"]}}},
        {"type": "function", "function": {
            "name": "web_fetch",
            "description": "Скачать веб-страницу и вернуть текст без тегов.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer"},
            }, "required": ["url"]}}},
        {"type": "function", "function": {
            "name": "memory_save",
            "description": "Заметка в долговременную память рабочего места. Пароли не сохранять.",
            "parameters": {"type": "object", "properties": {
                "note": {"type": "string"},
                "tags": {"type": "string"},
            }, "required": ["note"]}}},
        {"type": "function", "function": {
            "name": "memory_search",
            "description": "Полнотекстовый поиск по памяти рабочего места.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            }, "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "vault_search",
            "description": "Поиск по хранилищу доступов контура: метаданные БЕЗ секретов.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            }, "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "vault_get",
            "description": "Полная запись vault по id; reveal=true отдаёт секрет.",
            "parameters": {"type": "object", "properties": {
                "id": {"type": "integer"},
                "reveal": {"type": "boolean"},
            }, "required": ["id"]}}},
        {"type": "function", "function": {
            "name": "devrun_request",
            "description": "Поставить доработку самой Ноосферы в прогон: патч применяется в main сам, результат — системной заметкой в чат. Вызывай вместо «у меня нет инструментов».",
            "parameters": {"type": "object", "properties": {
                "task": {"type": "string"},
            }, "required": ["task"]}}},
        {"type": "function", "function": {
            "name": "run_list",
            "description": "Фоновые прогоны сервера (таблица runs) — единственный реестр прогонов контура.",
            "parameters": {"type": "object", "properties": {
                "limit": {"type": "integer"},
            }}}},
        {"type": "function", "function": {
            "name": "delegate",
            "description": "Под-агент с ЧИСТЫМ контекстом для подзадачи (разведка, проверка гипотезы): историю не видит — сформулируй задачу полностью, вернёт выжимку. Необратимое делай сам; вложенный delegate запрещён.",
            "parameters": {"type": "object", "properties": {
                "task": {"type": "string"},
            }, "required": ["task"]}}},
        {"type": "function", "function": {
            "name": "skill_list",
            "description": "Каталог скилов агента (имя + описание); тело — skill_load.",
            "parameters": {"type": "object", "properties": {
                "pattern": {"type": "string", "description": "опц. glob, напр. 1c-*"},
            }}}},
        {"type": "function", "function": {
            "name": "skill_load",
            "description": "Тело скила (SKILL.md) по точному имени из skill_list.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string"},
            }, "required": ["name"]}}},
        {"type": "function", "function": {
            "name": "mcp_tools",
            "description": "Список инструментов MCP-сервера контура.",
            "parameters": {"type": "object", "properties": {
                "server": {"type": "string"},
            }, "required": ["server"]}}},
        {"type": "function", "function": {
            "name": "mcp_call",
            "description": "Вызвать инструмент MCP-сервера; аргументы — JSON-строка по схеме из mcp_tools.",
            "parameters": {"type": "object", "properties": {
                "server": {"type": "string"},
                "tool": {"type": "string"},
                "arguments_json": {"type": "string"},
            }, "required": ["server", "tool", "arguments_json"]}}},
        # vault_save модели НЕ доступен (аудит r2, N-P1): карта доступов — только
        # человеком через REST; иначе инъекция переписывает записи.
    ]
    if not load_config().tools.shell:
        specs = [s for s in specs
                 if s["function"]["name"] not in ("shell_exec", "terminal_exec")]
    return specs


# Шапка и хвост списка MCP вынесены в константы: гейт smoke считает их как
# code-owned часть префикса даже там, где серверов нет (список — данные конфига).
MCP_HEADER = "MCP-серверы контура:\n"
MCP_TAIL = "\nНужен факт про контур — mcp_tools/mcp_call сюда, а не «нет доступа»."


def mcp_block() -> str:
    """Список MCP-серверов контура для системного промпта агента.

    Без него модель узнаёт о серверах только из vault и может не позвать MCP вовсе
    (бой 20.10: «посмотри в rlm» → четыре vault_search и десять локальных вызовов,
    ноль mcp_call, ответ «нужен доступ» — при живом rlm с 18 инструментами)."""
    try:
        from . import mcp
        rows = [s for s in mcp.servers_status() if s.get("enabled")]
    except Exception:
        return ""
    if not rows:
        return ""
    lines = [f"- {s['name']}: {s.get('desc') or 'без описания'}" for s in rows]
    return MCP_HEADER + "\n".join(lines) + MCP_TAIL


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[…обрезано, было {len(text)} символов]"


def _cap_ctx(text: str, ctx: dict | None) -> str:
    """Кап результата инструмента. У под-агента (ctx.depth) меньше общего:
    разведка обязана возвращать выжимку, а не пачку сырых файлов в окно.
    Берётся из tools.sub_output_cap."""
    cfg = load_config().tools
    cap = cfg.output_cap
    if (ctx or {}).get("depth"):
        cap = min(cap, int(cfg.sub_output_cap or 2000))
    return _cap(text, cap)


async def _shell_exec(args: dict, ctx: dict | None = None) -> str:
    cfg = load_config().tools
    if not cfg.shell:
        return "shell_exec отключён в конфиге (tools.shell: false)"
    if ctx is not None and ctx.get("allow_shell") is False:
        return "shell отключён для этого хода (фоновый прогон без allow_shell)"
    command = str(args.get("command") or "").strip()
    if not command:
        return "пустая команда"
    denied = _cmd_denied(command)
    if denied:
        return f"запрещённая команда (deny-list: {denied}) — нужен человек"
    timeout = min(int(args.get("timeout") or 30), cfg.shell_timeout)
    try:
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"команда не уложилась в {timeout}с — убита"
        text = out.decode("utf-8", errors="replace")
        return _cap_ctx(f"rc={proc.returncode}\n{text}" if proc.returncode else text or "(пустой вывод)",
                        ctx)
    except Exception as e:
        return f"shell_exec упал: {e!r}"


async def _terminal_exec(args: dict, ctx: dict | None = None) -> str:
    cfg = load_config().tools
    if not cfg.shell:
        return "terminal_exec отключён в конфиге (tools.shell: false)"
    if ctx is not None and ctx.get("allow_shell") is False:
        return "shell отключён для этого хода (фоновый прогон без allow_shell)"
    command = str(args.get("command") or "").strip()
    raw_input = args.get("input")
    actions = (bool(command), raw_input is not None,
               bool(args.get("poll")), bool(args.get("close")))
    if sum(actions) != 1:
        return "отказ: укажи ровно одно действие — command, input, poll или close"
    if args.get("enter") and raw_input is None:
        return "отказ: enter применяется только вместе с input"
    timeout = max(1, min(int(args.get("timeout") or 30), cfg.shell_timeout))
    tid = str(args.get("terminal_id") or (ctx or {}).get("session_id") or "default")
    try:
        if args.get("close"):
            closed = await terminals.kill(tid)
            return "терминал закрыт" if closed else "терминал уже закрыт"
        if args.get("poll"):
            return _cap_ctx(await terminals.poll_in(tid, timeout), ctx)
        guarded = command if command else str(raw_input)
        denied = _cmd_denied(guarded)
        if denied:
            return f"запрещённая команда (deny-list: {denied}) — нужен человек"
        if raw_input is not None:
            out = await terminals.input_in(
                tid, str(raw_input), bool(args.get("enter")), timeout)
        else:
            out = await terminals.exec_in(tid, command, timeout)
        return _cap_ctx(out, ctx)
    except Exception as e:
        return f"terminal_exec упал: {e!r}"


async def _read_file(args: dict) -> str:
    path = Path(str(args.get("path") or "")).expanduser()
    max_chars = min(int(args.get("max_chars") or 8000), 100_000)
    if not _jail_ok(path):
        return (f"путь вне джейла: {path} — чтение разрешено: ~/workspace, /tmp "
                "(+tools.read_allow), секреты только через vault_get")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"не прочитал {path}: {e}"
    return _cap(text, max_chars)


async def _write_file(args: dict) -> str:
    path = Path(str(args.get("path") or "")).expanduser()
    content = str(args.get("content") or "")
    if len(content) > 200_000:
        return f"отказ: content {len(content)} символов больше потолка 200000"
    if not _jail_ok(path, write=True):
        return (f"путь вне джейла для записи: {path} — запись разрешена: "
                "~/workspace, /tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception as e:
        return f"не записал {path}: {e}"
    return f"записано {len(content)} символов в {path}"


async def _web_fetch(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return "url должен начинаться с http:// или https://"
    max_chars = min(int(args.get("max_chars") or 8000), 50_000)
    max_bytes = 300_000  # лимит байт при чтении — не r.text целиком (F5)
    try:
        status, text = await _fetch_ssrf_safe(url, max_bytes)
    except ValueError as e:
        return f"запрещённый адрес: {e}"
    except Exception as e:
        return f"web_fetch упал: {e!r}"
    if "<" in text[:500]:
        text = _WS_RE.sub(" ", _TAG_RE.sub(" ", text))
    return _cap(f"http {status}\n{text.strip()}", max_chars)


async def _fetch_ssrf_safe(url: str, max_bytes: int) -> tuple[int, str]:
    """GET с SSRF-фильтром: приватные диапазоны запрещены, хост проверяется
    заново после КАЖДОГО редиректа (вручную, не follow_redirects=True)."""
    import ipaddress
    import socket
    from urllib.parse import urljoin, urlparse

    _BLOCKED = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("10.0.0.0/8"),
                ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("100.64.0.0/10"),
                ipaddress.ip_network("::1/128"), ipaddress.ip_network("fc00::/7"))

    def check(u: str) -> None:
        host = urlparse(u).hostname or ""
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise ValueError(f"хост не резолвится: {host}")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if any(ip in net for net in _BLOCKED):
                raise ValueError(f"{host} → {ip} из приватного диапазона")

    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        current = url
        for _ in range(4):  # до 3 редиректов
            check(current)
            r = await client.get(current, headers={"User-Agent": "noosphere-agent/1.0"})
            if r.is_redirect:
                current = urljoin(current, r.headers["location"])
                continue
            chunks: list[bytes] = []
            size = 0
            async for chunk in r.aiter_bytes(65536):
                size += len(chunk)
                if size > max_bytes:
                    chunks.append(chunk[: max(0, len(chunk) - (size - max_bytes))])
                    break
                chunks.append(chunk)
            enc = r.encoding or "utf-8"
            return r.status_code, b"".join(chunks).decode(enc, errors="replace")
        return 599, "слишком много редиректов"


async def _memory_save(args: dict) -> str:
    note = str(args.get("note") or "").strip()
    if not note:
        return "пустая заметка — не сохраняю"
    if len(note) > 4000:
        return f"заметка {len(note)} символов больше потолка 4000 — сожми суть"
    mem_id = memory.save(note, str(args.get("tags") or ""))
    return f"сохранено в память, id={mem_id}"


async def _memory_search(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "пустой запрос"
    rows = memory.search(query, min(int(args.get("limit") or 5), 20))
    if not rows:
        return "в памяти ничего не нашлось"
    return "\n".join(f"[{r['id']}] {r['ts'][:10]} {r['note']}"
                     + (f" (теги: {r['tags']})" if r["tags"] else "") for r in rows)


def _fmt_vault_row(r: dict) -> str:
    parts = [f"[{r['id']}] {r['kind']}/{r['name']}"]
    if r.get("host"):
        parts.append(r["host"])
    if r.get("login"):
        parts.append(f"логин: {r['login']}")
    if r.get("secret_file"):
        parts.append(f"секрет-файл: {r['secret_file']}")
    if r.get("has_secret"):
        parts.append("секрет: есть (vault_get reveal=true)")
    if r.get("notes"):
        parts.append(r["notes"])
    return " | ".join(parts)


async def _vault_search(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "пустой запрос"
    rows = vault.search(query, min(int(args.get("limit") or 10), 30))
    if not rows:
        return ("в хранилище ничего не нашлось — если узнал новый доступ, "
                "сообщи человеку: записи vault меняет только он через REST /v1/vault")
    return "\n".join(_fmt_vault_row(r) for r in rows)


async def _vault_get(args: dict, ctx: dict | None = None) -> str:
    row = vault.get(int(args.get("id") or 0))
    if row is None:
        return "запись не найдена"
    if not args.get("reveal"):
        row = dict(row)
        row["secret"] = "*** (reveal=true для раскрытия)"
    elif row.get("secret") and ctx is not None:
        # G1.5: раскрытое значение не должно оседать в снапшоте/FTS — ход
        # получает список строк для маскировки в sessions.complete_turn
        ctx.setdefault("reveal_secrets", []).append(row["secret"])
    return json.dumps(row, ensure_ascii=False, indent=1)


async def _vault_save(args: dict) -> str:
    # Недостижимо из спецификаций (vault_save не отдаётся модели), но страж
    # на случай угаданного имени: карта доступов меняется только человеком (REST).
    return "vault_save модели недоступен: записи меняет человек через REST /v1/vault"


async def _devrun_request(args: dict, ctx: dict | None = None) -> str:
    """Мост «чат → прогон доработки» (интент владельца 2026-10-13): агент ставит
    задачу в очередь devruns с пометкой chat:<sid>, оболочка ноутбука забирает её
    своим циклом и исполняет headless-qwen в изолированном клоне. Коммит и push
    делает владелец вручную — здесь только постановка."""
    from . import devruns
    task = str(args.get("task") or "").strip()
    if not task:
        return "отказ: devrun_request требует непустую task — суть доработки"
    sid = (ctx or {}).get("session_id") or "?"
    source = f"chat:{sid}"
    # дубль не ставим (бой 01.11: из одного чата два одинаковых прогона через
    # 7 с — первый закоммичен, второй умер «пустым патчем»)
    for q in devruns.queued_devruns(limit=20):
        if q.get("source") == source and (q.get("task") or "").strip() == task:
            return (f"Такой прогон уже в очереди: rid={q['id']} — дубль не ставлю. "
                    "Результат придёт в этот чат системной заметкой, когда прогон "
                    "финиширует.")
    rid = devruns.start(task, source=source)
    return (f"Прогон доработки поставлен в очередь: rid={rid}. Серверный "
            "исполнитель (devexec) берёт прогоны по одному в порядке очереди; "
            "ход виден во вкладке «Прогоны». Ответь человеку коротко: принял в "
            "прогон, очередь на сервере, по завершении результат придёт в этот "
            "чат системной заметкой и пушем, успешный патч применится в main "
            "автоматически (smoke-гейт, commit, push, рестарт с откатом). Не "
            "обещай проверку владельцем — этого гейта нет.")


async def _skill_list(args: dict) -> str:
    from . import skills
    rows = skills.catalog()
    pattern = str(args.get("pattern") or "").strip()
    if pattern:
        import fnmatch
        rows = [r for r in rows if fnmatch.fnmatchcase(r["name"], pattern)]
    if not rows:
        return "скилы не найдены" + (f" по паттерну {pattern!r}" if pattern else "")
    lines = [f"- {r['name']}: {r['desc']}" + ("" if r["enabled"] else " [выключен]")
             for r in rows[:150]]
    return _cap("\n".join(lines), 6000)


async def _skill_load(args: dict) -> str:
    from . import skills
    name = str(args.get("name") or "").strip()
    if not name:
        return "пустое имя скила"
    text = skills.load(name)
    if text is None:
        return f"скил {name!r} не найден — список через skill_list"
    return text


async def _mcp_tools(args: dict) -> str:
    from . import mcp
    server = str(args.get("server") or "").strip()
    if not server:
        known = ", ".join(s["name"] for s in mcp.servers_status()) or "(нет)"
        return f"пустое имя сервера; настроены: {known}"
    try:
        tl = await mcp.tools_list(server)
    except Exception as e:
        return f"mcp_tools({server}) упал: {e}"
    if not tl:
        return f"на сервере {server} инструментов нет"
    return _cap("\n".join(f"- {t['name']}: {t['description']}" for t in tl), 6000)


async def _mcp_call(args: dict) -> str:
    from . import mcp
    server = str(args.get("server") or "").strip()
    tool = str(args.get("tool") or "").strip()
    if not server or not tool:
        return "mcp_call требует server и tool"
    try:
        arguments = json.loads(str(args.get("arguments_json") or "{}"))
    except json.JSONDecodeError as e:
        return f"битый arguments_json: {e}"
    if not isinstance(arguments, dict):
        return "arguments_json должен быть JSON-объектом"
    try:
        return await mcp.call_tool(server, tool, arguments)
    except Exception as e:
        return f"mcp_call({server}/{tool}) упал: {e}"


_DELEGATE_MAX_OUT = 4000


async def _delegate(args: dict, ctx: dict | None = None) -> str:
    """Синхронный под-агент (28.10.2026): вложенный pool.ask с чистым контекстом и
    своим под-бюджетом (tools.sub_turn_budget) — широкая разведка не ест окно и
    бюджет координатора, назад уходит только выжимка. Глубина 1."""
    from . import pool
    task = str(args.get("task") or "").strip()
    if not task:
        return "отказ: delegate требует непустой task"
    ctx = ctx or {}
    if ctx.get("depth"):
        return "отказ: вложенный delegate запрещён (глубина 1) — верни выжимку сам"
    # тир под-агента выбирается явно: лёгкая формулировка подзадачи иначе уезжает
    # на base-классификацию и deepseek-flash с окном 131k, где под-бюджет и
    # компакция наступают почти одновременно (бой 02.11.2026)
    tier = str(args.get("tier") or "").strip() or load_config().tools.sub_tier
    if tier not in ("base", "mid", "deep", "max", "local"):
        return f"отказ: неизвестный тир {tier!r} — base|mid|deep|max|local"
    specs = [s for s in openai_specs()
             if (s.get("function") or {}).get("name") != "delegate"]
    child_ctx = {**ctx, "depth": 1, "session_id": None}

    async def child_exec(tc: dict) -> str:
        return await execute_tool_call(tc, ctx=child_ctx)

    system = (AGENT_SYSTEM +
              "\n\nТы — под-агент (delegate) внутри чужого хода: выполни подзадачу "
              "самостоятельно и верни выжимку для координатора — факты с цифрами и "
              "путями, что сделано, чего не хватило. Процесс и промежуточные вызовы "
              "не описывай. Спросить человека не можешь — реши сам или честно "
              "напиши, чего не хватило.\n\n"
              "Экономь бюджет: один shell-вызов читает несколько файлов сразу "
              "(cat a b c, grep -rn по каталогу), не больше 8 вызовов инструментов "
              "на подзадачу. Перечитывать то, что уже видел, запрещено.")
    res = await pool.ask("chat", task, system=system, tools=specs, tier=tier,
                         tool_executor=child_exec, turn_kind="sub",
                         session_id=ctx.get("session_id"), user=ctx.get("user"),
                         turn_budget=int(load_config().tools.sub_turn_budget))
    text = (res.get("text") or "").strip()
    if not text:
        return f"под-агент не ответил: {(res.get('err') or '?')[:300]}"
    usage = res.get("usage") or {}
    spent = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    foot = (f"\n[под-агент: {res.get('model', '?')}, {spent} tok"
            + (", НЕ ЗАКОНЧЕН: " + res.get("stop", "") if res.get("incomplete") else "")
            + "]")
    return _cap(text, _DELEGATE_MAX_OUT) + foot


async def _run_list(args: dict) -> str:
    from . import runs
    limit = int(args.get("limit") or 20)
    rows = runs.list_runs(limit)
    return json.dumps([{k: r.get(k) for k in
                        ("id", "session_id", "status", "created_at",
                         "finished_at", "err", "result_preview")}
                       for r in rows], ensure_ascii=False)


_EXECUTORS = {
    "shell_exec": _shell_exec,
    "devrun_request": _devrun_request,
    "run_list": _run_list,
    "terminal_exec": _terminal_exec,
    "read_file": _read_file,
    "write_file": _write_file,
    "web_fetch": _web_fetch,
    "memory_save": _memory_save,
    "memory_search": _memory_search,
    "vault_search": _vault_search,
    "vault_get": _vault_get,
    "vault_save": _vault_save,
    "skill_list": _skill_list,
    "skill_load": _skill_load,
    "mcp_tools": _mcp_tools,
    "mcp_call": _mcp_call,
    "delegate": _delegate,
    # Алиасы имён, которые модель выдумывает сама (замер 03.11 по tool_audit за
    # 30 дней: vault_rest — 32 вызова, shell_command — 15, плюс 4 с битым именем
    # 'shell_command" string="true'). Отказ «неизвестный инструмент» не лечился
    # текстом промпта: упоминание vault_rest в префлайте (cde8631) оставило те же
    # вызовы. Цена отказа — не сам отказ, а сожжённый раунд: каждый раунд
    # агентского цикла переотправляет модели весь контекст хода.
    # Состав и параметры инструментов НЕ меняются — openai_specs() алиасы не
    # объявляет, модель по-прежнему видит 16 канонических имён.
    "vault_rest": _vault_search,
    "shell_command": _shell_exec,
}

# Выдуманное имя → каноническое. Канонизация делается ДО исполнителя, чтобы
# ctx (allow_shell), args_summary и rc считались по каноническому имени, а не по
# тому, которое модель придумала: иначе shell_command не получил бы ctx и ушёл в
# «shell отключён», а rc у него не определялся бы (_infer_rc знает только канон).
_TOOL_ALIASES = {"vault_rest": "vault_search", "shell_command": "shell_exec"}
# Канонический состав — для текста отказа «неизвестный инструмент». Алиасы в нём
# не светим, иначе модель начнёт звать их намеренно; vault_save — тоже: модели он
# не отдаётся (аудит r2, N-P1), а упоминание в подсказке провоцирует вызов,
# который вернёт «недоступен» и сожжёт ещё один раунд.
_CANON_TOOLS = tuple(k for k in _EXECUTORS
                     if k not in _TOOL_ALIASES and k != "vault_save")


_REFUSE_PREFIXES = (
    "отказ:", "запрещён", "путь вне джейла", "неизвестный инструмент",
    "битые arguments", "shell_exec отключён", "terminal_exec отключён",
    "shell отключён", "vault_save модели недоступен",
)

_SECRET_IN_ARGS = (
    re.compile(r"(Bearer\s+)\S+", re.IGNORECASE),
    re.compile(r"(?i)(token|password|passwd|api[_-]?key|secret)([\"'\s]*[=:][\"'\s]*)\S+"),
)


def _mask_secrets(text: str) -> str:
    """Маскировка секретоподобных фрагментов в args_summary (G9): команда
    обрезается, токены/пароли заменяются на ***."""
    out = text
    for rx in _SECRET_IN_ARGS:
        out = rx.sub(lambda m: m.group(1) + (m.group(2) if m.lastindex >= 2 else "") + "***",
                     out)
    return out


def _args_summary(name: str, args: dict) -> str:
    """Обрезанный аргументный след БЕЗ значений секретов (G9): для shell —
    команда, для файлов — путь, для web_fetch — URL, для vault_get — факт
    reveal и id (сам секрет никогда)."""
    if name == "shell_exec":
        return _mask_secrets(str(args.get("command") or ""))[:200]
    if name == "terminal_exec":
        if "input" in args:
            suffix = " +enter" if args.get("enter") else ""
            return f"stdin <{len(str(args['input']))} chars>{suffix}"
        if args.get("poll"):
            return "poll"
        if args.get("close"):
            return "close"
        return _mask_secrets(str(args.get("command") or ""))[:200]
    if name in ("read_file", "write_file"):
        return _mask_secrets(str(args.get("path") or ""))[:200]
    if name == "web_fetch":
        return _mask_secrets(str(args.get("url") or ""))[:200]
    if name == "vault_get":
        return f"id={args.get('id')} reveal={bool(args.get('reveal'))}"
    if name in ("memory_save",):
        return _mask_secrets(str(args.get("note") or ""))[:80]
    if name in ("memory_search", "vault_search"):
        return _mask_secrets(str(args.get("query") or ""))[:80]
    if name == "skill_load":
        return f"name={args.get('name')}"
    if name == "skill_list":
        return f"pattern={args.get('pattern') or ''}"
    if name == "mcp_tools":
        return f"server={args.get('server')}"
    if name == "mcp_call":
        return f"{args.get('server')}/{args.get('tool')}"
    return _mask_secrets(json.dumps(args, ensure_ascii=False))[:200]


def _infer_rc(name: str, out: str) -> int | None:
    """Exit code команды из результата shell/terminal (rc=N — префикс при
    неудаче); None — не применимо/отказ до исполнения."""
    if name not in ("shell_exec", "terminal_exec"):
        return None
    if out.startswith("rc="):
        try:
            return int(out[3:out.index("\n")])
        except (ValueError, IndexError):
            return None
    if any(out.startswith(p) for p in _REFUSE_PREFIXES):
        return None
    if " упал:" in out[:80] or "не уложилась" in out[:80]:
        return None
    if "[процесс продолжается" in out:
        return None
    return 0


_SEARCH_TOOLS = ("grep", "egrep", "fgrep", "rg", "zgrep")


def is_benign_rc(name: str, rc: int | None, args_summary: str = "") -> bool:
    """rc=1 у grep/rg означает «совпадений нет», а не отказ команды (слово
    владельца 2026-11-01: 60 «отказов» shell_exec за сутки — в основном grep
    без совпадений в worktree исполнителя). Смотрим все сегменты цепочки:
    args_summary обрезан до 200 знаков и последний сегмент может быть срезан."""
    if name not in ("shell_exec", "terminal_exec") or rc != 1:
        return False
    segs = re.split(r"&&|\|\||[;|]", args_summary or "")
    for s in segs:
        head = s.strip().split(" ", 1)[0].rsplit("/", 1)[-1]
        if head in _SEARCH_TOOLS:
            return True
    return False


def _audit(name: str, ctx: dict | None, raw_args: str, args: dict | None,
           out: str, note: str) -> None:
    """Строка tool_audit на КАЖДЫЙ вызов и каждый отказ (G9). Провал журнала
    не роняет вызов, но и не глотается молча — crit в контур реакции."""
    try:
        db = get_db()
        db.execute(
            "INSERT INTO tool_audit (ts, session_id, turn_no, tool, args_digest,"
            " args_summary, out_len, out_digest, rc, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), (ctx or {}).get("session_id"), (ctx or {}).get("turn_no"),
             name, hashlib.sha256(raw_args.encode()).hexdigest()[:16],
             _args_summary(name, args or {}), len(out),
             hashlib.sha256(out.encode()).hexdigest()[:16],
             _infer_rc(name, out), note))
        db.commit()
    except Exception as e:
        try:
            from . import alerts
            alerts.send("crit", "tool_audit",
                        f"запись в tool_audit упала: {e!r} — журнал мёртв, "
                        "вызовы инструментов не фиксируются")
        except Exception:
            pass
        print(f"tool_audit write failed: {e!r}", file=__import__("sys").stderr)


async def execute_tool_call(tc: dict, ctx: dict | None = None) -> str:
    """Один tool_call OpenAI-формата → строка результата. Ошибки возвращаем
    текстом, а не исключением: модель должна увидеть провал и среагировать.
    ctx — контекст хода (session_id, turn_no, allow_shell, reveal_secrets).
    Каждый вызов И каждый отказ — строка в tool_audit (БЕЗ вывода и секретов)."""
    fn = (tc.get("function") or {})
    called_as = fn.get("name", "")
    name = _TOOL_ALIASES.get(called_as, called_as)
    raw_args = fn.get("arguments") or "{}"
    executor = _EXECUTORS.get(name)
    if executor is None:
        out = (f"неизвестный инструмент {called_as!r}: исправь имя или формат вызова — "
               "доступны " + ", ".join(sorted(_CANON_TOOLS)))
        _audit(called_as or "?", ctx, raw_args, None, out, "refuse")
        return out
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as e:
        out = f"битые arguments инструмента {name}: {e}"
        _audit(name, ctx, raw_args, None, out, "refuse")
        return out
    if name in ("terminal_exec", "shell_exec", "vault_get", "devrun_request", "delegate"):
        out = await executor(args, ctx)
    else:
        out = await executor(args)
    note = ""
    if any(out.startswith(p) for p in _REFUSE_PREFIXES):
        note = "refuse"
    _audit(name, ctx, raw_args, args, out, note)
    return out
