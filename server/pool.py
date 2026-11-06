"""pool.py — асинхронный транспорт к каналам LLM и маршрутизация задач (ask).

Транспорт портирован с llm-router/llm.py на httpx.AsyncClient; логика лестницы —
router.py; состояние (лимиты, бюджеты, fingerprint'ы) — SQLite через db.py/
antiloop.py, НЕ state.json (json расится под конкуренцией).

Железные правила (уроки llm-router и Paperclip):
 - повтор сети ≠ подъём модели: retryable → ретрай ТОГО ЖЕ тира после 2с;
 - обрыв finish=="length" → одна попытка с удвоенным max_tokens;
 - думающим тирам max_tokens ≥2048 (с инструментами ≥8192) — reasoning сжирает лимит до ответа;
 - provider-content-filter не ретраим ВСЛЕПУЮ (токены сгорят зря) — только эскалация;
 - завершённый ход НИКОГДА не повторяется на другом канале (Wrap-Up);
 - КАЖДАЯ попытка пишется в telemetry, включая провальные: ok = прошла проверка,
   answered = транспорт ответил — различать обязательно.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import uuid

import httpx

from . import accounts, antiloop, router, telemetry
from .config import load_config
from .db import get_db, today_local

CLIENT = httpx.AsyncClient()
_PROXY_CLIENTS: dict = {}


def _client_for(proxy: str) -> httpx.AsyncClient:
    """Клиент под канал: с proxy — отдельный (кэш по строке), без — общий CLIENT.

    30.10.2026: на маршруте Мака (TTK -> DE-CIX -> Cloudflare) с ночи дропаются
    TLS-рукопожатия OpenSSL-клиентов (Python/Node/openssl s_client) к SNI
    *.kimi.com, www.cloudflare.com, openai.com, api.anthropic.com; curl
    (LibreSSL), Windows (Schannel) и TLS1.2 проходят. Подписка и серверы Kimi
    живы (401/302 от обоих эндпоинтов). Обход по решению владельца — egress-
    прокси контура на канале kimi-cli (channels.kimi-cli.proxy). Диагностика:
    workspace/qwen/noosphere-personal-audit-2026-10-29/data/batch4{0,1,2,3}-*.txt
    """
    if not proxy:
        return CLIENT
    c = _PROXY_CLIENTS.get(proxy)
    if c is None:
        c = httpx.AsyncClient(proxy=proxy)
        _PROXY_CLIENTS[proxy] = c
    return c

# Zen (opencode Go API) требует стабильный session-заголовок на процесс,
# иначе 400 MissingSessionID (живая проба 2026-10-13).
_ZEN_SESSION = uuid.uuid4().hex

_URL_RE = re.compile(r"https?://\S+")
# rate-limit РАНЬШЕ auth: Kimi заворачивает исчерпание квоты в 403.
_RATE_MARKERS = ("rate limit", "rate_limit", "ratelimit", "too many requests",
                 "quota", "insufficient_quota", "throttl", "превышен лимит")
_AUTH_MARKERS = ("unauthorized", "invalid api key", "invalid_api_key", "forbidden",
                 "authentication", "access denied", "недействительн")
_FILTER_MARKERS = ("data_inspection_failed", "content filter", "content_filter",
                   "content_moderation", "sensitive")

# Ход идёт до результата; ограничивают его ресурсы (время, токены) и детекторы
# отсутствия прогресса, а не счётчик раундов. Когда бюджет всё-таки кончился —
# последний вызов идёт БЕЗ инструментов, чтобы ход отдал итог, а не пустой текст.
WRAP_UP_PROMPT = ("Бюджет хода исчерпан, но инструменты в порядке и снова будут "
                  "доступны следующим сообщением — это не поломка. Ответь СЕЙЧАС, без "
                  "инструментов, по исходной задаче: что установлено ФАКТАМИ, что "
                  "осталось непроверенным и какое ПЕРВОЕ действие сделать дальше. "
                  "Не пиши «когда вернутся инструменты» и не выдавай диагностику за починку.")

# Автопродолжение обрыва вывода (порт из канона 29.10.2026: дайджесты и длинные
# обзоры резались на полуслове — обрыв finish=length на потолке выдавался как финал).
# Частичный текст уходит assistant-репликой, модель дописывает с места обрыва,
# куски склеиваются. Не применяется к check_json.
CONTINUE_PROMPT = ("Ответ оборвался по лимиту длины. Продолжи СТРОГО с места обрыва: "
                   "без повторов уже написанного, без комментариев об обрыве и без "
                   "заново написанных заголовков — только продолжение текста.")

# Ход без инструментов (28.10.2026, разбор пустых ответов): DeepSeek Flash станции
# даже без tools в запросе пытается вызвать инструмент, шлюз выбрасывает разметку —
# приходит content:null, текста нет. Ставится в system wrap-up вызова.
NO_TOOLS_NOTE = ("В этом ходе инструментов нет: команды, файлы и сервисы недоступны. "
                 "Ничего не вызывай — отвечай текстом сразу, по тому, что знаешь; чего не "
                 "проверить без инструментов — так и скажи.")

# Компакция (28.10.2026): окно ≥70% или бюджет токенов исчерпан — модель фиксирует
# состояние, история заменяется на [задача + состояние], ход ПРОДОЛЖАЕТСЯ вместо
# смерти по бюджету. Потолок компакций — страховка от цикла «сжал → разведка → сжал».
COMPACT_PROMPT = ("Контекст хода заполнен — зафиксируй состояние для продолжения в новом, "
                  "пустом контексте. Ответь СТРОГО пунктами: ЗАДАЧА (одной строкой), ФАКТЫ "
                  "(что установлено: цифры, пути, идентификаторы — без процесса), СДЕЛАНО, "
                  "СЛЕДУЮЩИЙ ШАГ (что именно делать дальше первым делом). До 1500 знаков.")
COMPACT_MAX = 5
# E3 (аудит 2026-10-29): бюджет хода — не приговор, пока ход идёт вперёд.
# Замер: 23 обрыва по бюджету за 7 дней, из них 10 — под-агент на 100k и 7 — ход
# на 400k, при том что настоящий детектор отсутствия прогресса (antiloop:
# fingerprint повторов и treading-water) за те же 7 дней не сработал НИ РАЗУ.
# То есть ходы резала арифметика, а не бесполезность. Продлеваем бюджет, только
# если с прошлой отметки появились новые раунды инструментов, и не больше двух
# раз: от runaway по-прежнему держат turn_max_minutes и antiloop.
BUDGET_EXT_MAX = 2
COMPACT_WINDOW_PCT = 70

# Вторая причина компакции — абсолютный размер контекста хода (замер 03.11).
# Порог «70% окна» на станции (131 072) = 91 750 токенов, а максимум наблюденного
# контекста чат-хода за 7 дней — 77 134 (p90 29 192, p50 11 405): по окну компакция
# не срабатывала НИ РАЗУ (в телеметрии 7 «compact #1» за 7 дней — все по бюджету
# токенов). Ходы росли до страховочного потолка раундов, а расход хода квадратичен
# по их числу: 10 ходов с ≥40 раундами дали 44,5% всего расхода чата (19,7M из
# 44,4M токенов), 32 хода с ≥20 раундами (15% ходов) — 73%.
# Ужатие ОТДЕЛЬНЫХ сообщений истории (_shrink_tool_history) для этой цели не
# годится и сознательно не трогается: правка старого сообщения сбрасывает
# префикс-кэш провайдера (91,5% prompt-токенов чата — cache_read, свежих 1 922 на
# вызов), то есть ~1,5k свежих токенов на раунд превращаются в десятки тысяч.
# Полная компакция платит за сброс кэша один раз и дальше снова идёт по кэшу.
# Канон — tools.compact_above_tokens в config.example.yaml; 0 выключает.
COMPACT_ABOVE_TOKENS_DEFAULT = 24_000


def _compact_above_tokens() -> int:
    """Порог компакции по абсолютному контексту: конфиг, иначе дефолт."""
    try:
        return int(getattr(load_config().tools, "compact_above_tokens",
                           COMPACT_ABOVE_TOKENS_DEFAULT) or 0)
    except (TypeError, ValueError):
        return COMPACT_ABOVE_TOKENS_DEFAULT


# Последние N результатов инструментов в истории — целиком, старшие усекаются до
# выжимки: иначе каждый раунд переотправляет весь накопленный дамп (ход 20.10 из
# 12 раундов по ~8 КБ результатов сжёг 338 960 токенов).
TOOL_KEEP_FULL = 4
TOOL_STUB_CHARS = 400
# E1 (аудит 2026-10-29): ужатие старых результатов — только когда история реально
# велика. Раньше _shrink_tool_history вызывался в КАЖДОМ раунде и каждый раунд
# переписывал одно сообщение (оно выходило из окна последних четырёх), то есть
# префикс менялся на каждом шаге и кэш провайдера сбрасывался. Ниже порога история
# уходит в модель байт в байт как в прошлом раунде — префикс попадает в кэш;
# защита от квадратичного роста сохранена (ход 20.10 из 12 раундов сжёг 338 960).
TOOL_SHRINK_AFTER_CHARS = 60_000
# Маркер уже урезанного результата: без него сообщение длиннее TOOL_STUB_CHARS
# (400 + сам маркер ≈ 450) резалось ЗАНОВО в каждом раунде, причём текст маркера
# менялся («было 30000 зн.» → «было 448 зн.»), то есть префикс истории плыл на
# каждом шаге и кэш провайдера сбрасывался ровно там, где ужатие и включилось.
TOOL_SHRUNK_MARK = "…[результат усечён в истории хода"


def _empty_no_tools(res: dict, tools: list | None) -> bool:
    """Транспорт ответил 200, но ни текста, ни вызовов: ход без инструментов, где модель
    пыталась что-то вызвать (разбор 28.10.2026). Сетевые сбои (status) — не сюда.
    Порт из канона gitlab 29.10.2026 — без него пустые ответы станции на ходах без
    инструментов (route/chat) шли прямо в treading-water."""
    return (not tools and not res.get("ok") and not res.get("status")
            and not res.get("text") and not res.get("tool_calls")
            and res.get("err") == "пустой ответ")


def _tool_history_chars(messages: list[dict]) -> int:
    """Суммарный объём результатов инструментов в истории хода."""
    total = 0
    for m in messages:
        if m.get("role") == "tool":
            body = m.get("content")
            if isinstance(body, str):
                total += len(body)
    return total


def _shrink_tool_history(messages: list[dict]) -> bool:
    """Усекает СОДЕРЖИМОЕ старых tool-сообщений, не трогая роли и tool_call_id:
    пары assistant.tool_calls ↔ tool остаются целыми для протокола.

    Возвращает True, если история изменена. До порога TOOL_SHRINK_AFTER_CHARS не
    трогает ничего: префикс остаётся байт в байт как в прошлом раунде и попадает
    в кэш провайдера (E1, аудит 2026-10-29)."""
    if _tool_history_chars(messages) <= TOOL_SHRINK_AFTER_CHARS:
        return False
    idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    changed = False
    for i in idx[:-TOOL_KEEP_FULL]:
        body = messages[i].get("content")
        if (isinstance(body, str) and len(body) > TOOL_STUB_CHARS
                and TOOL_SHRUNK_MARK not in body):
            messages[i] = {**messages[i],
                           "content": body[:TOOL_STUB_CHARS]
                           + f"\n…[результат усечён в истории хода, было {len(body)} зн.]"}
            changed = True
    return changed


def _turn_budget_exceeded(tools_cfg, tool_iters: int, max_tool_iters: int,
                          t_start: float, spent_tokens: int,
                          budget_tokens: int = 0, label: str = "хода") -> str:
    """Почему ход обязан остановиться. Рабочие бюджеты — время и токены хода;
    счётчик раундов — только страховка от безумия, а не рабочий лимит.
    budget_tokens — переопределение (под-агент delegate со своим под-бюджетом,
    тогда label «под-агента» — порт из канона 29.10)."""
    budget = budget_tokens or tools_cfg.turn_max_tokens
    if tool_iters >= max_tool_iters:
        return f"страховочный потолок раундов ({max_tool_iters})"
    minutes = (time.time() - t_start) / 60
    if minutes >= tools_cfg.turn_max_minutes:
        return f"бюджет времени хода ({tools_cfg.turn_max_minutes} мин)"
    if spent_tokens >= budget:
        return f"бюджет токенов {label} ({budget})"
    return ""


def _alert(severity: str, kind: str, text: str, meta: dict | None = None) -> int:
    try:
        from . import alerts
        return alerts.send(severity, kind, text, meta)
    except ImportError:
        print(f"noosphere-alert[{severity}/{kind}]: {text}", file=sys.stderr)
        return 0
    except Exception as e:  # сломанный контур реакции не должен ронять вызовы LLM
        print(f"noosphere-alert: send упал ({e!r}); текст: {text}", file=sys.stderr)
        return 0


def classify_error(status: int, text: str) -> str:
    """Класс сбоя провайдера по статусу и телу. URL вырезаем: в них попадают
    токены и ложные маркеры. Неизвестное → "unknown", чтобы НЕ гасить канал
    по непонятной ошибке."""
    t = _URL_RE.sub(" ", text or "").lower()
    if status == 400 and "data_inspection_failed" in t:
        return "provider-content-filter"
    if status == 429 or any(m in t for m in _RATE_MARKERS):
        return "rate-limit"
    if any(m in t for m in _FILTER_MARKERS):
        return "provider-content-filter"
    if status in (401, 403) or any(m in t for m in _AUTH_MARKERS):
        return "auth"
    if status == 0:
        return "network"
    if status and status >= 500:
        return "server"
    return "unknown"


# Отказ МАРШРУТА, а не провайдера: только такие классы имеют смысл повторять
# другим транспортом. Замер 02.11 (turns, 14 дней): 12 × ProxyError('503
# Forwarding failure') за 7 минут 01.11 убили ночного судью целиком — privoxy
# отдавал 503, пока туннель egress openclaw-nl-socks делал 4611 рестартов за
# сутки; 24 × ConnectError('') 30.10 — наоборот, прямой маршрут дропал TLS к
# *.kimi.com, и обходным стал тот же privoxy. Оба транспорта независимы,
# поэтому провайдерские 4xx/5xx здесь НЕ повторяем: это уже ответ сервера.
_TRANSPORT_MARKERS = ("proxyerror", "connecterror", "connecttimeout",
                      "forwarding failure")


def _transport_failure(err: str) -> bool:
    t = (err or "").lower()
    return any(m in t for m in _TRANSPORT_MARKERS)


async def _post(url: str, payload: dict, *, key: str = "", timeout: int = 180,
                extra_headers: dict | None = None, proxy: str = "",
                alt_proxy: str | None = None) -> tuple[int, dict | str]:
    """POST с запасным маршрутом. `alt_proxy` — транспорт повторной попытки
    ("" = напрямую, None = повторять нечем). Прокси и прямое соединение — два
    независимых маршрута к одному провайдеру: отказ одного не означает отказ
    канала, и ход не должен умирать вместе с чужим туннелем."""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    if extra_headers:
        headers.update(extra_headers)

    async def _once(via: str) -> tuple[int, dict | str]:
        try:
            r = await _client_for(via).post(url, json=payload, headers=headers,
                                            timeout=timeout)
        except Exception as e:  # сеть/таймаут — статус 0, как в llm.py
            return 0, repr(e)[:200]
        if r.status_code != 200:
            return r.status_code, r.text[:400]
        try:
            return 200, r.json()
        except Exception:
            return 200, {}

    st, resp = await _once(proxy)
    if st == 0 and alt_proxy is not None and alt_proxy != proxy \
            and _transport_failure(str(resp)):
        label = "напрямую" if not alt_proxy else f"через {alt_proxy}"
        st2, resp2 = await _once(alt_proxy)
        if st2 == 0:
            return 0, (f"{str(resp)[:110]} | запасной маршрут ({label}): "
                       f"{str(resp2)[:110]}")
        print(f"pool: egress-failover — маршрут {proxy or 'прямой'} отказал "
              f"({str(resp)[:80]}), запрос прошёл {label}", file=sys.stderr, flush=True)
        return st2, resp2
    return st, resp


def _strip_tools_from_messages(msgs: list[dict]) -> list[dict]:
    """История без tool-следов для вызовов с tools=None (BACKLOG-15 п.19).

    Станция за litellm (Anthropic-трансформ) отвергает запрос, где в сообщениях
    есть tool_calls/role=tool, а параметра tools= нет: 400
    UnsupportedParamsError «Anthropic doesn't support tool calling without
    tools=» (воспроизведено пробами 29.10: форма «история с инструментами без
    tools» → 400, та же история с tools → 200). Wrap-up, компакция, continue и
    fallback идут БЕЗ инструментов, но с полной историей хода — 18 провальных
    ходов 28–29.10. Ряд «вызов → результат» сворачивается в ОДНУ
    assistant-реплику текстом: провайдеру это обычная история, а модель в
    wrap-up видит, что именно инструменты вернули.
    """
    out: list[dict] = []
    names: dict = {}
    buf: list | None = None

    def flush() -> None:
        nonlocal buf
        if buf is not None:
            out.append({"role": "assistant",
                        "content": "\n".join(buf) or "(работа инструментами)"})
        buf = None

    for m in msgs:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            if buf is None:
                base = str(m.get("content") or "").strip()
                buf = [base] if base else []
            for tc in m["tool_calls"] or []:
                fn = (tc or {}).get("function") or {}
                nm = str(fn.get("name") or "?")
                names[(tc or {}).get("id")] = nm
                buf.append(f"[вызвал {nm} {str(fn.get('arguments') or '')[:200]}]")
        elif role == "tool":
            if buf is None:
                buf = []
            body = m.get("content")
            if not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False)
            nm = names.get(m.get("tool_call_id"), "инструмент")
            buf.append(f"[{nm} →] {body}")
        else:
            flush()
            if isinstance(m, dict) and "tool_calls" in m:
                m = {k: v for k, v in m.items() if k != "tool_calls"}
            out.append(m)
    flush()
    return out


# Модели, которые reasoning'уют независимо от флагов тела (замер 30.10:
# станционный flash игнорирует enable_thinking:false и сжигает кап до ответа).
# Для них пол max_tokens 2048 ставится по префиксу модели.
THINKY_PREFIXES = ("local/deepseek", "k3", "kimi-")

# v1.8: системка vision-«глаз»: описание картинок для текстового исполнителя
EYES_SYSTEM = ("Ты — глаза текстового исполнителя. Опиши приложенные изображения как можно "
               "точнее: тексты, элементы интерфейса и их состояния (раскрыто/свёрнуто, "
               "выделено, обрезано), цифры, сообщения об ошибках. В конце одним абзацем — "
               "что просят сделать с учётом увиденного. Без выводов за пределами видимого.")


async def call(channel_name: str, tier: str, messages: list[dict], max_tokens: int = 1024,
               system: str | None = None, spec: dict | None = None,
               tools: list[dict] | None = None, timeout: int | None = None) -> dict:
    """Один вызов тира через канал. Возвращает {ok,text,model,usage,finish,err,tier,status?}.
    С tools — ещё tool_calls и assistant_message (сырое сообщение OpenAI для истории).
    `spec` — переопределение тира ТОЛЬКО для этого вызова (глобальный TIERS не мутируем)."""
    ch = load_config().channel(channel_name)
    spec = spec or router.TIERS[tier]
    if ch is None:
        return {"ok": False, "text": "", "model": spec["model"], "usage": {},
                "finish": "", "err": f"канал {channel_name} не настроен", "tier": tier}
    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    if not tools:
        # п.19: история с tool_calls без tools= — гарантированный 400 на станции
        msgs = _strip_tools_from_messages(msgs)
    if ch.kind == "ollama":
        # v1.6: content может быть массивом (мультимодальность) — для ollama
        # тексты склеиваем, картинки уходят в отдельный параметр images.
        texts: list[str] = []
        images: list[str] = []
        for m in msgs:
            c = m["content"]
            if isinstance(c, str):
                texts.append(c)
                continue
            for part in c:
                if part.get("type") == "text":
                    texts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    if "," in url:
                        images.append(url.split(",", 1)[1])
        body = {"model": spec["model"], "prompt": "\n".join(texts),
                "stream": False, "options": {"num_predict": max_tokens}}
        if images:
            body["images"] = images
        st, resp = await _post(ch.base_url.rstrip("/") + "/api/generate", body, timeout=120)
        if st == 200 and isinstance(resp, dict):
            return {"ok": True, "text": resp.get("response", ""), "model": spec["model"],
                    "usage": {}, "finish": "stop", "tier": tier}
        return {"ok": False, "text": "", "model": spec["model"], "usage": {},
                "finish": "", "err": f"local {st} {resp}", "tier": tier, "status": st}

    body = {"model": spec["model"], "messages": msgs, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
    body.update(spec["body"])  # enable_thinking/reasoning_effort — это и есть цена
    extra = {"x-opencode-session": _ZEN_SESSION} if ch.kind == "zen" else None
    if ch.kind == "kimi":
        # подписка Kimi Code вызывается не из CLI — отдаём UA настоящего
        # клиента, как в рефреше токена (accounts.py): голый python-httpx палит
        extra = {**(extra or {}), "User-Agent": "kimi-code/0.39"}
    # думающие стадии живут дольше 180с (бой 13.10: ReadTimeout спеки): таймаут
    # приходит со стадии (ROLES процессов), дефолт — 600с zen, 180с остальные
    timeout = timeout or (600 if ch.kind == "zen" else 180)
    # запасной маршрут — прямое соединение: прокси канала это обход чужого
    # туннеля, а не часть контракта с провайдером (см. _TRANSPORT_MARKERS)
    st, resp = await _post(ch.base_url.rstrip("/") + "/chat/completions", body,
                           key=accounts.api_key(ch), timeout=timeout, extra_headers=extra,
                           proxy=ch.proxy, alt_proxy="" if ch.proxy else None)
    if st != 200 or not isinstance(resp, dict):
        return {"ok": False, "text": "", "model": spec["model"], "usage": {},
                "finish": "", "err": f"http {st} {resp}", "tier": tier, "status": st}
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    return {"ok": bool(text) or bool(tool_calls), "text": text,
            "model": resp.get("model", spec["model"]),
            "usage": resp.get("usage") or {}, "finish": choice.get("finish_reason", ""),
            "err": "" if (text or tool_calls) else "пустой ответ", "tier": tier,
            "reasoning": msg.get("reasoning_content") or "",
            "tool_calls": tool_calls,
            "assistant_message": {"role": "assistant", **msg}}


async def _local_up() -> bool:
    ch = load_config().channel("ollama")
    if not ch or not ch.enabled:
        return False
    try:
        r = await CLIENT.get(ch.base_url.rstrip("/") + "/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def _usage_parts(usage: dict) -> tuple[int, int, int, int]:
    u = usage or {}
    rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
    cr = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return u.get("prompt_tokens", 0), u.get("completion_tokens", 0), rt, cr


def _tier_calls_today(tier: str) -> int:
    """Счётчик дневного лимита тира — псевдоканал tier:<name> в budgets
    (та же атомарность, без новой таблицы)."""
    row = get_db().execute("SELECT spent_calls FROM budgets WHERE day = ? AND channel = ?",
                           (today_local(), f"tier:{tier}")).fetchone()
    return row["spent_calls"] if row else 0


async def _vision_eyes(ch, prompt: str, images: list[dict], *, session_id, user,
                       task_class, turn_kind, rec, start) -> tuple:
    """v1.8: ОДИН vision-вызов БЕЗ инструментов — модель подписки не принимает
    tools вместе с изображениями (проба 01.11: 400, ход сполз на фолбэк без tools).
    Возвращает (model, описание, ошибка); нет канала/отказ — ("", "", причина)."""
    if ch is None:
        return "", "", "vision-канал не настроен"
    model = ch.vision_model or "qwen3.7-plus"
    allowed, _level = antiloop.check_budget(ch.name)
    if not allowed:
        return "", "", "дневной бюджет vision-канала исчерпан"
    content: list[dict] = [{"type": "text", "text": prompt}]
    for im in images:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{im['mime']};base64,{im['b64']}"}})
    t0 = time.time()
    res = await call(ch.name, "vision", [{"role": "user", "content": content}], 2048,
                     system=EYES_SYSTEM, spec={"model": model, "body": {}})
    ms = int((time.time() - t0) * 1000)
    pt, ct, rt, cr = _usage_parts(res.get("usage"))
    rec(telemetry.TurnRec(
        session_id=session_id, user=user, task_class=task_class, tier_start=start,
        tier_used="vision", model=res.get("model", model), channel=ch.name,
        ok=bool(res.get("ok")), answered=bool(res.get("ok")), why="vision-eyes",
        finish=res.get("finish", ""), prompt_tokens=pt, completion_tokens=ct,
        reasoning_tokens=rt, cache_read_tokens=cr, ms=ms, err=res.get("err", ""),
        escalated=False, content_filter=False, turn_kind=turn_kind))
    if res.get("ok"):
        antiloop.note_spend(ch.name, pt + ct)
        return res.get("model", model), res.get("text", ""), ""
    return model, "", res.get("err", "") or "пустой ответ"


async def _ask_impl(task_class: str, prompt: str, *, system: str | None = None, tier: str | None = None,
              max_tokens: int | None = None, check_json: bool = False, min_chars: int = 0,
              must: str | None = None, think: bool = False, force_no_escalate: bool = False,
              session_id: str | None = None, user: str | None = None,
              history: list[dict] | None = None, tools: list[dict] | None = None,
              tool_executor=None, max_tool_iters: int | None = None, turn_kind: str = "",
              force_channel: str | None = None, force_spec: dict | None = None,
              timeout: int | None = None, turn_budget: int | None = None,
              images: list[dict] | None = None, role: str = "",
              turn_uid: str = "") -> dict:
    """Маршрутизация одной задачи: стартовый тир по классу → эскалация при провале.
    history — предыдущие ходы сессии (sessions.get_history): без неё модель не
    видит контекст диалога (P0-5 аудита 11.10).
    tools + tool_executor — агентский цикл: ответ с tool_calls исполняется,
    результат уходит обратно в модель (до max_tool_iters итераций на ход).
    force_no_escalate (G3): обрыв лестницы и на ПРОПУСКЕ тира (лимит/бюджет/
    выключен), не только после неудачной попытки — классификатор не уезжает
    на дорогой тир при исчерпании base.
    force_channel + force_spec (v1.7, процессы 1С): роль стадии жёстко привязана
    к каналу и модели (спека→alibaba/deepseek-v4-pro, судья→kimi-cli/k3.1-pro) — лестница и оверрайды
    класса не применяются, эскалация отключена (force_no_escalate подразумевается).
    role (замер 02.11): маршрут и запасные задаёт роль (router.roles), а не
    лестница тиров — base и mid это одна модель, поэтому эскалация по ним ничего
    не меняла, а deep уводил чат на модель судьи. Цепочка роли проходит свои
    запасные даже при force_no_escalate (это явный маршрут, а не подъём тира) и
    НЕ дополняется глобальным fallback_tier. Приоритет: force_channel > role >
    tier= > CLASS_TIER; tier= при настроенной роли остаётся меткой в телеметрии.
    usage в результате — СУММА по всем итерациям агентского цикла (G10, P1-7:
    раньше писалась только последняя итерация).
    Результат: {ok,text,model,tier,trace,escalated,class,usage,ms,err}."""
    cfg = load_config()
    # v21 (BACKLOG-15 п.3): все записи телеметрии одного хода связаны turn_uid,
    # а итоговая строка несёт агрегат — см. _rec() ниже и _finish_turn() в обёртке ask().
    turn_uid = turn_uid or uuid.uuid4().hex[:12]
    turn_seq = 0

    def _rec(rec: telemetry.TurnRec) -> int:
        nonlocal turn_seq
        turn_seq += 1
        rec.turn_uid = turn_uid
        rec.turn_seq = turn_seq
        return telemetry.record_turn(rec)

    if max_tool_iters is None:
        max_tool_iters = cfg.tools.max_iters  # единственный источник правды — конфиг,
        # а не дефолт сигнатуры: раньше 6 в подписи молча перекрывал config.yaml
    budget_tokens = int(turn_budget or 0) or cfg.tools.turn_max_tokens  # delegate: под-бюджет
    compact_above = _compact_above_tokens()
    start = tier or router.start_tier(task_class)
    chain = router.role_chain(role) if role else []
    chain_mode = bool(chain)
    if max_tokens is None:
        max_tokens = (chain[0]["max_tokens"] if chain_mode else 0) or cfg.router.max_tokens
    if think and start == "base":
        # «base, но подумай»: правим ЛОКАЛЬНУЮ копию — один think не должен
        # сделать base «думающим» на весь процесс.
        tiers = dict(router.TIERS)
        tiers["base"] = {"model": "deepseek-v4.1-flash",
                         "body": {"enable_thinking": True, "reasoning_effort": "high"}}
    else:
        tiers = router.TIERS
    limits = cfg.router.limits or {}
    trace: list[dict] = []
    res: dict = {}
    budget_blocked = False
    t_start = time.time()
    if images:
        # v1.8: картинки читает vision-вызов без инструментов, его описание уходит в
        # промпт обычного агентского цикла с инструментами (см. _vision_eyes)
        vch = next((c for c in cfg.channels.values() if c.enabled and c.vision), None)
        eyes_model, eyes_text, eyes_err = await _vision_eyes(
            vch, prompt, images, session_id=session_id, user=user,
            task_class=task_class, turn_kind=turn_kind, rec=_rec, start=start)
        if eyes_text:
            prompt = (f"{prompt}\n\n[Вложения просмотрены vision-моделью {eyes_model}; "
                      "её описание вместо картинок]:\n" + eyes_text)
        else:
            prompt = (f"{prompt}\n\n[Вложения просмотреть не удалось: "
                      f"{eyes_err or 'vision-модель не дала описания'}]")
        images = None
    fp_prompt = (system or "") + "\n" + prompt
    messages = list(history or []) + [{"role": "user", "content": prompt}]
    tool_iters = 0
    last_text = ""                     # последний непустой текст модели — для итога/фолбэка
    milestones: set[int] = set()       # вехи бюджета хода, о которых модель уже предупреждена
    rotated_classes: set[str] = set()
    tot_pt = tot_ct = tot_rt = 0  # сумма usage по ВСЕМ итерациям хода (G10)
    spent_turn = 0                     # расход с последней компакции — по нему стоп и вехи
    compactions = 0
    budget_ext = 0                     # сколько раз бюджет продлён по прогрессу (E3)
    iters_at_budget = 0                # раундов инструментов на момент последнего продления
    # заполненность окна для индикатора чата = вход ПЕРВОГО вызова хода (вся история +
    # сообщение — «сколько всего в чате»); последний вызов после компакции несёт
    # урезанный контекст и врёт в шапке (порт из канона 29.10)
    ctx = {"tokens": 0, "window": 0, "first": 0}

    def _agg_usage() -> dict:
        return {"prompt_tokens": tot_pt, "completion_tokens": tot_ct,
                "reasoning_tokens": tot_rt,
                "context_tokens": ctx["first"] or ctx["tokens"],
                "context_window": ctx["window"]}

    # В цепочке роли каждый шаг — свой маршрут, тир в телеметрии остаётся
    # стартовым (tier_start/tier_used не врут о цене задачи, а смену маршрута
    # показывает escalated и channel/model).
    if chain_mode:
        ladder = [start] * len(chain)
    elif force_channel:
        ladder = [start]
    else:
        ladder = router.LADDER[router.LADDER.index(start):]
    for tier_idx, tier_name in enumerate(ladder):
        forced = force_channel is not None or chain_mode
        esc = (tier_name != start) or tier_idx > 0
        lim = 0 if forced else int(limits.get(tier_name, 0))  # 0 = без лимита
        if lim and _tier_calls_today(tier_name) >= lim:
            trace.append({"tier": tier_name, "skip": "дневной лимит исчерпан"})
            if force_no_escalate and not chain_mode:
                break
            continue
        if tier_name == "local" and not forced and not await _local_up():
            trace.append({"tier": "local", "skip": "ollama не поднят или выключен"})
            if force_no_escalate and not chain_mode:
                break
            continue
        if chain_mode:
            entry = chain[tier_idx]
            channel = entry["channel"]
            spec_ov = {"model": entry["model"], "body": entry["body"]}
        elif force_channel:
            channel, spec_ov = force_channel, None
        else:
            channel, spec_ov = router.channel_of_tier(tier_name)
        ch = cfg.channel(channel)
        if ch is None or not ch.enabled:
            trace.append({"tier": tier_name, "skip": f"канал {channel} выключен"})
            if force_no_escalate and not chain_mode:
                break
            continue
        allowed, _level = antiloop.check_budget(channel)
        if not allowed:
            budget_blocked = True
            trace.append({"tier": tier_name, "skip": "дневной бюджет канала исчерпан"})
            if force_no_escalate and not chain_mode:
                break
            continue
        spec = force_spec or dict(tiers.get(tier_name) or {"model": "", "body": {}})
        if spec_ov:
            spec = {**spec, **spec_ov}  # канал-оверрайд тира несёт свою модель/body
        # оверрайд модели направлением не применяется к явно заданному маршруту:
        # роль и force_spec сильнее ночного оптимизатора
        ov_model = None if (force_spec or chain_mode) else router.model_override_for(task_class)
        if ov_model and tier_name != "local":
            # направление диктует модель внутри неб-локального канала («глм с 1С
            # справляется плохо»): override заменяет модель тира, тело тира остаётся
            spec = {**spec, "model": ov_model}
        attempt_tokens = max_tokens
        spec_body = spec["body"]
        if (spec_body.get("enable_thinking") or "reasoning_effort" in spec_body
                or str(spec["model"]).startswith(THINKY_PREFIXES)):
            # думающий тир: в пробе reasoning сжёг все 400 токенов до ответа;
            # станционный flash и kimi k3.1-pro reasoning'уют даже при
            # enable_thinking:false (налог замерен 30.10: 90 пустых route-ответов
            # и 50 обрывов по max_tokens) — пол считаем и по префиксу модели,
            # а не только по флагам тела. С инструментами пол выше (замер 01.11:
            # code-ходы с промптом 20k+ reasoning съедает кап 4096 целиком —
            # 9 пустых ответов, ретрай на 8192 успешен; пол убирает первый
            # вызов впустую)
            attempt_tokens = max(attempt_tokens, 8192 if tools is not None else 2048)
        fp = antiloop.fingerprint_of(spec["model"], fp_prompt)
        attempt = 0
        length_retries = 0
        empty_retries = 0
        max_attempt_tokens = attempt_tokens * 4
        hit_length_limit = False
        call_system = system
        while True:
            t0 = time.time()
            _shrink_tool_history(messages)  # старые результаты — выжимкой: иначе контекст растёт квадратично
            res = await call(channel, tier_name, messages,
                             attempt_tokens, call_system, spec=spec, tools=tools,
                             timeout=timeout)
            ms = int((time.time() - t0) * 1000)
            err_class = "" if res.get("ok") else classify_error(res.get("status", 0), res.get("err", ""))
            if res.get("ok") and res.get("tool_calls"):
                ok, why = True, "tool_calls"  # текст при вызове инструментов может быть пустым — это норма
            else:
                ok, why = router.passes(res, check_json, min_chars, must)
            if res.get("text"):
                last_text = res["text"]
            # КАЖДАЯ попытка — в телеметрию и в анти-цикл, включая ретраи:
            # иначе по журналу не отличить «дорого» от «часто».
            pt, ct, rt, cr = _usage_parts(res.get("usage"))
            tot_pt += pt
            tot_ct += ct
            tot_rt += rt
            # в бюджет хода идёт только СВЕЖАЯ часть prompt: кэш-префикс провайдеру
            # стоит копейки, а раньше считался как новая работа — delegate умирал на
            # 100k, перечитывая один и тот же контекст (бой 02.11.2026)
            spent_turn += max(pt - cr, 0) + ct
            if pt:
                ctx["tokens"] = pt
                if not ctx["first"]:
                    ctx["first"] = pt   # вход первого вызова = размер всего чата
                ctx["window"] = int(getattr(ch, "context_window", 0) or 0)
            if tools is not None and budget_tokens:
                # вехи бюджета (бой 28.10: ход жёг 400k на разведку и ушёл в wrap-up
                # без единого факта): на 50% и 80% — явное «хватит перебирать»
                spent_pct = spent_turn * 100 / budget_tokens
                for mark in (50, 80):
                    if spent_pct >= mark and mark not in milestones:
                        milestones.add(mark)
                        note = (f"Бюджет хода израсходован на {mark}%: если это всё ещё "
                                "разведка — прекращай перебор: действуй по задаче, спроси "
                                "человека (ask_user) или подводи итог по собранному.")
                        call_system = f"{call_system}\n\n{note}" if call_system else note
            _rec(telemetry.TurnRec(
                session_id=session_id, user=user, task_class=task_class, tier_start=start,
                tier_used=tier_name, model=res.get("model", ""), channel=channel,
                ok=ok, answered=bool(res.get("ok")), why=why, finish=res.get("finish", ""),
                prompt_tokens=pt, completion_tokens=ct, reasoning_tokens=rt,
                cache_read_tokens=cr, ms=ms, err=res.get("err", ""),
                escalated=esc,
                content_filter=(err_class == "provider-content-filter"),
                turn_kind=turn_kind))
            antiloop.note_spend(f"tier:{tier_name}", 0, calls=1)
            if res.get("ok"):
                antiloop.note_spend(channel, pt + ct)
            if res.get("tool_calls"):
                # итерация агента — ни провал, ни сброс счётчика fingerprint (R2 аудита r2:
                # ok=True обнулял счётчик и слепил treading-water на самом дорогом пути)
                stop = False
            elif res.get("finish") == "length" and (res.get("text") or "").strip():
                # обрыв по длине С ТЕКСТОМ — рост max_tokens и автопродолжение, а не
                # топтание: в антицикл не считаем (порт из канона 29.10.2026; бой «слоны»)
                stop = False
            else:
                stop, _cnt = antiloop.note_attempt(fp, ok)
            if stop:
                # класс route: у классификатора есть безопасный fallback (route_intent → run),
                # crit с обязательным ack непропорционален (бой 28.10: красная плашка
                # «подтверди реакцию» на фоне пустых ответов станции)
                _alert("warning" if task_class == "route" else "crit", "treading_water",
                       f"один и тот же вход провалился ≥{cfg.antiloop.max_repeat_fingerprint} раз "
                       f"(класс {task_class}, тир {tier_name})"
                       + (" — остановлено" if cfg.antiloop.stop_enabled
                          else " — продолжаем (стоп-режим выключен 04.11)"),
                       {"class": task_class, "tier": tier_name, "session_id": session_id})
                if cfg.antiloop.stop_enabled:
                    trace.append({"tier": tier_name, "model": res.get("model"), "ok": False,
                                  "why": "treading-water: стоп"})
                    return {"ok": False, "text": "", "model": res.get("model", ""), "tier": tier_name,
                            "trace": trace, "escalated": esc, "class": task_class,
                            "usage": _agg_usage(), "ms": int((time.time() - t_start) * 1000),
                            "err": "treading-water: стоп"}
                trace.append({"tier": tier_name, "model": res.get("model"),
                              "ok": bool(res.get("ok")),
                              "why": "antiloop: повтор входа — алерт, ход продолжается"})
            if err_class == "provider-content-filter":
                break  # вслепую не ретраим — токены сгорят зря; только эскалация
            if err_class in ("auth", "rate-limit") and err_class not in rotated_classes:
                # пул аккаунтов (FOUNDING §9): отказ → cooldown + следующий аккаунт,
                # ретрай ТОГО ЖЕ вызова с новым ключом (ход ещё не завершён).
                rotated_classes.add(err_class)
                new_idx = await asyncio.to_thread(accounts.rotate, ch)
                if new_idx is not None:
                    trace.append({"tier": tier_name, "rotate_account": new_idx,
                                  "why": f"{err_class} — аккаунт на cooldown {accounts.COOLDOWN_MINUTES} мин"})
                    continue
                trace.append({"tier": tier_name, "why": f"{err_class} — ротировать некуда"})
            if ok and res.get("tool_calls") and tool_executor is not None:
                # агентский цикл: исполняем вызовы и отдаём результаты той же модели
                allowed, _level = antiloop.check_budget(channel)
                if not allowed:
                    # бюджет проверяется не только на входе в тир, но и на каждой
                    # платной итерации (R2: 6 платных вызовов через одну проверку)
                    trace.append({"tier": tier_name, "model": res.get("model"), "ok": False,
                                  "why": "дневной бюджет канала исчерпан на итерации агента"})
                    return {"ok": False, "text": res.get("text", ""), "model": res.get("model", ""),
                            "tier": tier_name, "trace": trace, "escalated": esc,
                            "class": task_class, "usage": _agg_usage(),
                            "ms": int((time.time() - t_start) * 1000),
                            "err": "дневной бюджет канала исчерпан"}
                stop_why = _turn_budget_exceeded(cfg.tools, tool_iters, max_tool_iters,
                                                 t_start, spent_turn, budget_tokens,
                                                 label="под-агента" if turn_budget
                                                 else "хода")
                if (stop_why.startswith("бюджет токенов") and budget_tokens
                        and budget_ext < BUDGET_EXT_MAX
                        and tool_iters > iters_at_budget):
                    # ход дошёл до бюджета, но с прошлой отметки РАБОТАЛ: новые раунды
                    # инструментов. Отсутствие прогресса ловит antiloop, а не арифметика.
                    budget_ext += 1
                    budget_tokens = int(budget_tokens * 2)
                    iters_at_budget = tool_iters
                    trace.append({"tier": tier_name, "model": res.get("model"), "ok": True,
                                  "why": f"{stop_why} — бюджет продлён до {budget_tokens}: "
                                         f"ход идёт вперёд ({tool_iters} раундов, "
                                         f"продление {budget_ext}/{BUDGET_EXT_MAX})"})
                    milestones.clear()          # вехи считаются от нового бюджета
                    stop_why = ""
                win_full = bool(ctx["window"]) and \
                    ctx["tokens"] * 100 >= ctx["window"] * COMPACT_WINDOW_PCT
                # второй триггер — абсолютный размер контекста: процент окна
                # недостижим на ходу, который упирается в потолок раундов раньше
                ctx_full = bool(compact_above) and ctx["tokens"] >= compact_above
                if (stop_why.startswith("бюджет токенов") or win_full or ctx_full) \
                        and compactions < COMPACT_MAX:
                    # Компакция: модель фиксирует состояние, история заменяется на
                    # [задача + состояние], ход продолжается со свежим окном — вместо
                    # смерти по бюджету (бой 28.10: «продолжай» стартовал с амнезией).
                    compactions += 1
                    # res.assistant_message с tool_calls НЕ добавляем: API требует за
                    # tool_calls ответы tool — вместо них плоская пометка о намерении
                    pending = ", ".join((tc.get("function") or {}).get("name", "?")
                                        for tc in (res.get("tool_calls") or []))
                    comp_msgs = list(messages)
                    if res.get("text"):
                        comp_msgs.append({"role": "assistant", "content": res["text"]})
                    if pending:
                        comp_msgs.append({"role": "assistant",
                                          "content": f"(собирался вызвать: {pending})"})
                    comp_msgs.append({"role": "user", "content": COMPACT_PROMPT})
                    comp_system = f"{system}\n\n{NO_TOOLS_NOTE}" if system else NO_TOOLS_NOTE
                    t0 = time.time()
                    comp = await call(channel, tier_name, comp_msgs, attempt_tokens,
                                      comp_system, spec=spec, tools=None, timeout=timeout)
                    comp_ms = int((time.time() - t0) * 1000)
                    cpt, cct, crt, ccr = _usage_parts(comp.get("usage"))
                    tot_pt += cpt
                    tot_ct += cct
                    tot_rt += crt
                    _rec(telemetry.TurnRec(
                        session_id=session_id, user=user, task_class=task_class,
                        tier_start=start, tier_used=tier_name, model=comp.get("model", ""),
                        channel=channel, ok=bool(comp.get("ok")), answered=bool(comp.get("ok")),
                        why=f"compact #{compactions}", finish=comp.get("finish", ""),
                        prompt_tokens=cpt, completion_tokens=cct, reasoning_tokens=crt,
                        cache_read_tokens=ccr, ms=comp_ms, err=comp.get("err", ""),
                        escalated=esc, turn_kind=turn_kind))
                    if comp.get("ok"):
                        antiloop.note_spend(channel, cpt + cct)
                    if comp.get("ok") and (comp.get("text") or "").strip():
                        messages[:] = [{"role": "user", "content": prompt},
                                       {"role": "assistant", "content": comp["text"].strip()},
                                       {"role": "user", "content":
                                        "Продолжай по этому состоянию — инструменты снова "
                                        "доступны. Уже сделанное не повторяй."}]
                        trace.append({"tier": tier_name, "model": comp.get("model"), "ok": True,
                                      "why": f"компакция #{compactions}: окно было "
                                             f"{ctx['tokens']} ток, расход {spent_turn} — "
                                             "ход продолжается со свежим контекстом"})
                        spent_turn = cpt + cct
                        milestones.clear()
                        iters_at_budget = tool_iters   # прогресс считаем от компакции
                        ctx["tokens"] = 0           # переизмерится следующим вызовом
                        continue                    # отложенные tool_calls модель перевыпустит
                    # компакция не удалась — дальше обычный wrap-up
                    if not stop_why:
                        if win_full:
                            stop_why = f"окно контекста заполнено (≥{COMPACT_WINDOW_PCT}%)"
                        elif ctx_full:
                            stop_why = f"контекст хода ≥{compact_above} токенов"
                if stop_why:
                    # Бюджет исчерпан — не пустой ответ: финальный вызов БЕЗ
                    # инструментов просит подвести итог (бой 20.10: задача умирала
                    # на середине пустым текстом).
                    trace.append({"tier": tier_name, "model": res.get("model"), "ok": False,
                                  "why": f"{stop_why} — итог без инструментов"})
                    wrap_msgs = list(messages)
                    if res.get("text"):
                        wrap_msgs.append({"role": "assistant", "content": res["text"]})
                    wrap_msgs.append({"role": "user", "content": WRAP_UP_PROMPT})
                    # без NO_TOOLS_NOTE шлюз станции выбрасывает разметку вызова инструмента
                    # и отвечает пустым content (разбор 28.10)
                    wrap_system = f"{system}\n\n{NO_TOOLS_NOTE}" if system else NO_TOOLS_NOTE

                    async def _wrap(msgs: list[dict]) -> dict:
                        nonlocal tot_pt, tot_ct, tot_rt
                        t0 = time.time()
                        w = await call(channel, tier_name, msgs, attempt_tokens,
                                       wrap_system, spec=spec, tools=None, timeout=timeout)
                        wrap_ms = int((time.time() - t0) * 1000)
                        wpt, wct, wrt, wcr = _usage_parts(w.get("usage"))
                        tot_pt += wpt
                        tot_ct += wct
                        tot_rt += wrt
                        _rec(telemetry.TurnRec(
                            session_id=session_id, user=user, task_class=task_class,
                            tier_start=start, tier_used=tier_name, model=w.get("model", ""),
                            channel=channel, ok=bool(w.get("ok")), answered=bool(w.get("ok")),
                            why=f"wrap-up: {stop_why}", finish=w.get("finish", ""),
                            prompt_tokens=wpt, completion_tokens=wct, reasoning_tokens=wrt,
                            cache_read_tokens=wcr, ms=wrap_ms, err=w.get("err", ""),
                            escalated=esc, turn_kind=turn_kind))
                        if w.get("ok"):
                            antiloop.note_spend(channel, wpt + wct)
                        return w

                    wrap = await _wrap(wrap_msgs)
                    if not wrap.get("ok"):
                        # повтор на урезанном контексте: полная история хода могла сама
                        # быть причиной провала (переполнение окна; бой 28.10 — «итог без
                        # инструментов не удался» на бюджете 400k)
                        small = [{"role": "user", "content": prompt}]
                        if last_text:
                            small.append({"role": "assistant", "content": last_text})
                        small.append({"role": "user", "content": WRAP_UP_PROMPT +
                                      " Контекст хода усечён — итог по тому, что видишь, "
                                      "и честно скажи, какие детали потеряны."})
                        wrap = await _wrap(small)
                    if wrap.get("ok"):
                        note = (f"⚠ Ход не закончен: {stop_why}. Инструменты в порядке — "
                                "продолжить следующим сообщением.\n\n")
                        return {"ok": True, "text": note + wrap.get("text", ""),
                                "model": wrap.get("model", ""), "tier": tier_name,
                                "trace": trace, "escalated": esc,
                                "class": task_class, "usage": _agg_usage(),
                                "ms": int((time.time() - t_start) * 1000), "err": "",
                                "incomplete": True, "stop": stop_why}
                    # оба итога провалились — детерминированный текст вместо красного
                    # баннера без ответа (бой 28.10)
                    _alert("warning", "wrap_up",
                           f"wrap-up хода провалился дважды ({stop_why}): "
                           f"{(wrap.get('err') or '?')[:200]}",
                           {"class": task_class, "tier": tier_name, "session_id": session_id})
                    fallback = (f"⚠ Ход остановлен: {stop_why}. Автоитог моделью не получен "
                                f"({(wrap.get('err') or '?')[:200]}) — инструменты в порядке, "
                                "продолжить можно следующим сообщением.")
                    if last_text:
                        fallback += "\n\nПоследний текст модели:\n" + last_text
                    return {"ok": True, "text": fallback, "model": res.get("model", ""),
                            "tier": tier_name, "trace": trace, "escalated": esc,
                            "class": task_class, "usage": _agg_usage(),
                            "ms": int((time.time() - t_start) * 1000), "err": "",
                            "incomplete": True, "stop": stop_why}
                tool_iters += 1
                messages.append(res.get("assistant_message") or
                                {"role": "assistant", "content": res["text"],
                                 "tool_calls": res["tool_calls"]})
                for tc in res["tool_calls"]:
                    result = await tool_executor(tc)
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                     "content": result})
                continue
            if _empty_no_tools(res, tools) and empty_retries < 1:
                # пустой ответ хода без инструментов: модель пыталась вызвать инструмент,
                # шлюз выбросил разметку (см. NO_TOOLS_NOTE). Один повтор того же тира
                # с явным «инструментов нет» — тир не поднимаем (порт из канона 29.10).
                empty_retries += 1
                if NO_TOOLS_NOTE not in (call_system or ""):
                    call_system = f"{call_system}\n\n{NO_TOOLS_NOTE}" if call_system else NO_TOOLS_NOTE
                trace.append({"tier": tier_name, "model": res.get("model"), "ok": False,
                              "why": "пустой ответ без инструментов — повтор того же тира"})
                continue
            if router.retryable(res) and attempt < 1:
                attempt += 1
                await asyncio.sleep(2)
                continue
            if res.get("finish") == "length" and attempt_tokens < max_attempt_tokens:
                # обрыв вывода — не отказ модели и не сеть: поднимаем max_tokens
                # в пределах того же тира (до 4x), а не тир.
                length_retries += 1
                attempt_tokens *= 2
                continue
            hit_length_limit = res.get("finish") == "length"
            break

        if hit_length_limit and not check_json and (res.get("text") or "").strip() \
                and not res.get("tool_calls"):
            # порт из канона 29.10.2026: обрыв на потолке роста max_tokens — не финал,
            # текст доезжает автопродолжением (дайджесты резались на полуслове)
            cont_max = int(getattr(cfg.router, "continue_max", 2) or 0)
            for cont_no in range(1, cont_max + 1):
                cont_msgs = list(messages)
                cont_msgs.append({"role": "assistant", "content": res["text"]})
                cont_msgs.append({"role": "user", "content": CONTINUE_PROMPT})
                t0 = time.time()
                cont = await call(channel, tier_name, cont_msgs, attempt_tokens,
                                  call_system, spec=spec, tools=None, timeout=timeout)
                cont_ms = int((time.time() - t0) * 1000)
                pt, ct, rt, cr = _usage_parts(cont.get("usage"))
                tot_pt += pt
                tot_ct += ct
                tot_rt += rt
                _rec(telemetry.TurnRec(
                    session_id=session_id, user=user, task_class=task_class,
                    tier_start=start, tier_used=tier_name, model=cont.get("model", ""),
                    channel=channel, ok=bool(cont.get("ok")), answered=bool(cont.get("ok")),
                    why=f"continuation #{cont_no}", finish=cont.get("finish", ""),
                    prompt_tokens=pt, completion_tokens=ct, reasoning_tokens=rt,
                    cache_read_tokens=cr, ms=cont_ms, err=cont.get("err", ""),
                    escalated=esc, turn_kind=turn_kind))
                if cont.get("ok"):
                    antiloop.note_spend(channel, pt + ct)
                piece = cont.get("text") or ""
                if not cont.get("ok") or not piece.strip():
                    trace.append({"tier": tier_name, "model": cont.get("model"),
                                  "ok": False, "why": f"продолжение #{cont_no} не удалось: "
                                                      f"{(cont.get('err') or 'пусто')[:120]}"})
                    break
                res["text"] += piece
                trace.append({"tier": tier_name, "model": cont.get("model"), "ok": True,
                              "why": f"продолжение #{cont_no} обрыва"})
                if cont.get("finish") != "length":
                    # res несёт finish исходного обрыва — без замены passes() считает
                    # ответ оборванным и ход эскалирует несмотря на доехавший текст
                    # (бой 29.10: «слоны» base→mid→deep после успешного продолжения)
                    res["finish"] = cont.get("finish") or "stop"
                    hit_length_limit = False
                    ok, why = router.passes(res, check_json, min_chars, must)
                    break

        if hit_length_limit and not check_json and not ok and (res.get("text") or "").strip():
            # продолжения исчерпаны, а текст всё ещё обрывается — отдаём собранное
            # с честной пометкой вместо пустой ошибки (контракт incomplete — как wrap-up)
            note = (f"⚠ Ответ оборван по лимиту длины (max_tokens {attempt_tokens}, "
                    "продолжения исчерпаны) — скажи «продолжи», допишу.\n\n")
            trace.append({"tier": tier_name, "model": res.get("model"), "ok": True,
                          "why": "обрыв после продолжений — текст с пометкой"})
            return {"ok": True, "text": note + res["text"], "model": res.get("model", ""),
                    "tier": tier_name, "trace": trace, "escalated": False, "class": task_class,
                    "usage": _agg_usage(), "ms": int((time.time() - t_start) * 1000), "err": "",
                    "incomplete": True, "stop": f"обрыв по max_tokens ({attempt_tokens})"}
        if hit_length_limit and not ok:
            trace.append({"tier": tier_name, "model": res.get("model"), "ok": False,
                          "why": f"поднят max_tokens до {attempt_tokens} — всё равно обрыв, "
                                 "тир не поднимаем"})
            return {"ok": False, "text": "", "model": res.get("model", ""), "tier": tier_name,
                    "trace": trace, "escalated": False, "class": task_class,
                    "usage": _agg_usage(), "ms": int((time.time() - t_start) * 1000),
                    "err": f"обрыв по max_tokens ({attempt_tokens})"}

        trace.append({"tier": tier_name, "model": res.get("model"), "ok": ok, "why": why})
        if ok:
            return {"ok": True, "text": res["text"], "model": res.get("model", ""),
                    "tier": tier_name, "trace": trace, "escalated": esc,
                    "class": task_class, "usage": _agg_usage(),
                    "ms": int((time.time() - t_start) * 1000), "err": "",
                    "reasoning": res.get("reasoning", "")}
        if chain_mode and tier_idx + 1 < len(chain):
            nxt = chain[tier_idx + 1]
            trace.append({"tier": tier_name, "model": res.get("model"), "ok": False,
                          "why": f"роль {role}: {channel}/{spec.get('model')} не ответил "
                                 f"({(res.get('err') or why)[:120]}) — запасной маршрут "
                                 f"{nxt['channel']}/{nxt['model']}"})
        if force_no_escalate and not chain_mode:
            break

    # E6 (аудит 2026-10-29): подписка — не единственная опция. Если лестница
    # кончилась отказом (429 месячной квоты, 401 ключ, 5xx, «нет доступного тира»),
    # ход доигрывается на запасном бесплатном тире, а не возвращается пустой
    # ошибкой: владельцу нужен ответ, а не сообщение о том, что квота кончилась.
    # Инструменты в запасном ходе выключены — это последний шанс ответить по тому,
    # что уже собрано, а не начинать новый агентский цикл на чужом канале.
    res = res or {}
    fb_tier = (getattr(cfg.router, "fallback_tier", "") or "").strip()
    # chain_mode исключён намеренно: у роли свои запасные маршруты, а глобальный
    # fallback_tier это бесплатная станция — для судьи она даёт вердикт, который
    # в отчёте неотличим от настоящего (01.11: 6 таких попыток, все с ошибкой).
    if fb_tier and not chain_mode and not res.get("ok") and fb_tier != tier_name:
        fb_channel, fb_ov = router.channel_of_tier(fb_tier)
        fb_ch = cfg.channel(fb_channel)
        if fb_ch is not None and fb_ch.enabled and fb_channel != channel:
            fb_spec = dict(tiers.get(fb_tier) or {"model": "", "body": {}})
            if fb_ov:
                fb_spec = {**fb_spec, **fb_ov}
            trace.append({"tier": fb_tier, "model": fb_spec.get("model"), "ok": False,
                          "why": f"лестница кончилась ({res.get('err') or 'нет ответа'}) — "
                                 f"запасной ход на {fb_channel}"})
            t0 = time.time()
            fbres = await call(fb_channel, fb_tier, messages, attempt_tokens, call_system,
                               spec=fb_spec, tools=None, timeout=timeout)
            fb_ms = int((time.time() - t0) * 1000)
            fpt, fct, frt, fcr = _usage_parts(fbres.get("usage"))
            tot_pt += fpt
            tot_ct += fct
            tot_rt += frt
            _rec(telemetry.TurnRec(
                session_id=session_id, user=user, task_class=task_class, tier_start=start,
                tier_used=fb_tier, model=fbres.get("model", ""), channel=fb_channel,
                ok=bool(fbres.get("ok")), answered=bool(fbres.get("ok")),
                why=f"fallback: {res.get('err') or 'лестница кончилась'}",
                finish=fbres.get("finish", ""), prompt_tokens=fpt, completion_tokens=fct,
                reasoning_tokens=frt, cache_read_tokens=fcr, ms=fb_ms,
                err=fbres.get("err", ""), escalated=True, turn_kind=turn_kind))
            if fbres.get("ok"):
                antiloop.note_spend(fb_channel, fpt + fct)
                trace.append({"tier": fb_tier, "model": fbres.get("model"), "ok": True,
                              "why": f"запасной канал {fb_channel} ответил"})
                return {"ok": True, "text": fbres.get("text", ""),
                        "model": fbres.get("model", ""), "tier": fb_tier,
                        "trace": trace, "escalated": True, "class": task_class,
                        "usage": _agg_usage(), "ms": int((time.time() - t_start) * 1000),
                        "err": "", "reasoning": fbres.get("reasoning", ""),
                        "fallback": fb_channel}
            trace.append({"tier": fb_tier, "model": fbres.get("model"), "ok": False,
                          "why": f"запасной канал тоже отказал: {(fbres.get('err') or '')[:120]}"})
            res = res or fbres

    ms_total = int((time.time() - t_start) * 1000)
    if res:
        err = res.get("err") or "проверка не пройдена"
    elif budget_blocked:
        err = "дневной бюджет канала исчерпан"
    else:
        err = "нет доступного тира"
    return {"ok": False, "text": "", "model": res.get("model", ""), "tier": res.get("tier", ""),
            "trace": trace, "escalated": True, "class": task_class,
            "usage": _agg_usage(), "ms": ms_total, "err": err}


def _finish_turn(turn_uid: str, res: dict, ms_total: int) -> int:
    """Итоговая строка хода: turn_last=1 + агрегат расхода (BACKLOG-15 п.3).

    Агрегат берётся из res["usage"] — это _agg_usage(), сумма по ВСЕМ итерациям
    хода. Если ход упал исключением и результата нет, сумму собираем из уже
    записанных попыток: расход не должен исчезать вместе с трейсбеком.
    Кэш-токены отдельной колонкой не дублируем — они и так лежат в каждой
    попытке и суммируются по turn_uid.
    """
    c = get_db()
    usage = (res or {}).get("usage") or {}
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    rt = int(usage.get("reasoning_tokens") or 0)
    ms = int((res or {}).get("ms") or ms_total or 0)
    if not usage:
        row = c.execute(
            "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c,"
            " COALESCE(SUM(reasoning_tokens),0) r, COALESCE(SUM(ms),0) m"
            " FROM turns WHERE turn_uid=?", (turn_uid,)).fetchone()
        if row:
            pt, ct, rt, ms = int(row["p"]), int(row["c"]), int(row["r"]), int(row["m"])
    cur = c.execute(
        "UPDATE turns SET turn_last=1, turn_prompt_tokens=?, turn_completion_tokens=?,"
        " turn_reasoning_tokens=?, turn_ms=?"
        " WHERE id=(SELECT MAX(id) FROM turns WHERE turn_uid=?)", (pt, ct, rt, ms, turn_uid))
    c.commit()
    if cur.rowcount == 0:
        print(f"pool: ход {turn_uid} не оставил строк в turns", flush=True)
    return cur.rowcount


async def ask(*args, **kwargs) -> dict:
    """Ход целиком: телеметрия попыток + итоговая строка с агрегатом расхода.

    Обёртка над _ask_impl (сигнатура та же). До неё record_turn писал каждую
    попытку отдельно и без признака принадлежности к ходу: за день 506 строк при
    48 POST /v1/chat, и ответить «сколько сжёг этот ход» по БД было нельзя —
    максимум prompt_tokens по строкам 23 427 при бюджете хода 400 000. Теперь у
    попыток общий turn_uid, есть turn_seq, а на строке с turn_last=1 лежат
    суммарные токены и время хода; число ходов за день — COUNT(*) WHERE
    turn_last=1, а не число строк.
    """
    uid = uuid.uuid4().hex[:12]
    vision_dropped = False
    if kwargs.get("images"):
        # v1.8: картинки не теряем молча — нет vision-канала → ход идёт текстом
        # с инструментами, но владелец видит пометку и алерт
        vch = next((c for c in load_config().channels.values()
                    if c.enabled and c.vision), None)
        if vch is None:
            vision_dropped = True
            kwargs.pop("images", None)
    t0 = time.time()
    res: dict = {}
    try:
        res = await _ask_impl(*args, turn_uid=uid, **kwargs)
        res["vision_dropped"] = vision_dropped
        return res
    finally:
        _finish_turn(uid, res, int((time.time() - t0) * 1000))
