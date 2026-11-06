"""router.py — чистая логика маршрутизации по тирам (порт llm-router/llm.py).

Без транспорта и состояния: лестница тиров, класс задачи → стартовый тир,
проверка результата, решение о ретрае. Транспорт — pool.py, состояние — db.py.

Смысл лестницы: на КАЖДУЮ задачу берётся самый дешёвый достаточный тир,
подъём — только при провале. `body` тира (enable_thinking/reasoning_effort) —
это то, что реально управляет ценой вызова.
"""
from __future__ import annotations

import json
import sys

TIERS: dict[str, dict] = {
    "local": {"model": "qwen3:8b", "body": {}, "local": True},
    "base": {"model": "deepseek-v4.1-flash", "body": {"enable_thinking": False}},
    "mid": {"model": "qwen3.8-flash", "body": {"reasoning_effort": "low"}},
    "deep": {"model": "deepseek-v4-pro", "body": {"enable_thinking": True, "reasoning_effort": "high"}},
    "max": {"model": "qwen3.8-max", "body": {"reasoning_effort": "low"}},
}
LADDER = ["local", "base", "mid", "deep", "max"]

# Тир → канал транспорта (имя в config.channels).
# 20.10.2026: низ лестницы переехал с payg-канала qwencloud на подписку Token Plan
# (free quota Model Studio кончилась — payg больше не жжём; приказ владельца).
CHANNEL_OF_TIER = {"local": "ollama", "base": "alibaba", "mid": "alibaba",
                   "deep": "alibaba", "max": "alibaba"}


def channel_of_tier(tier: str) -> tuple[str, dict | None]:
    """Тир → (канал, оверрайд спеки {model?, body?}). Дефолт — CHANNEL_OF_TIER;
    переопределение конфигом router.tier_channels: {tier: "канал"} или
    {tier: {channel, model?, body?}} — подключение локального канала инстанса
    (напр. бесплатный vLLM заказчика) без правки кода."""
    from .config import load_config
    ov = (load_config().router.tier_channels or {}).get(tier)
    if isinstance(ov, str) and ov:
        return ov, None
    if isinstance(ov, dict) and ov.get("channel"):
        spec_ov = {k: v for k, v in ov.items() if k in ("model", "body")}
        return str(ov["channel"]), spec_ov or None
    return CHANNEL_OF_TIER[tier], None


def _role_entry(item: dict) -> dict:
    return {"channel": str(item["channel"]), "model": str(item["model"]),
            "body": dict(item.get("body") or {}),
            "max_tokens": int(item.get("max_tokens") or 0)}


def role_chain(role: str) -> list[dict]:
    """Роль → явная цепочка маршрутов [{channel, model, body, max_tokens}].

    Первый элемент — основной, остальные (`failover`) — запасные в порядке
    перечисления. Пустой список = роль не настроена, тогда работает лестница
    тиров (обратная совместимость: конфиг без roles ведёт себя как раньше).

    Зачем (замер 02.11, turns за 7 дней): лестница перестала быть лестницей —
    `base` и `mid` это один station/local/deepseek-flash (4858 из 5136 вызовов),
    поэтому подъём base→mid не менял модель, а следующий шаг `deep` уводил
    обычный чат и исполнителя прогонов на kimi-cli/k3.1-pro — модель ночного судьи с
    окном 100 вызовов на 5 часов. Роль задаёт модель явно и запасные маршруты
    перечисляет явно. Судья при этом НЕ падает на бесплатную флеш-модель:
    вердикт слабой модели в отчёте неотличим от настоящего, а замер 30 дней
    даёт среднюю confidence 0,043 у deepseek-v4-pro против 0,719 у k3.
    """
    from .config import load_config

    raw = (load_config().router.roles or {}).get(role)
    if not raw:
        return []
    if isinstance(raw, str):
        raw = {"channel": raw}
    if not isinstance(raw, dict) or not raw.get("channel") or not raw.get("model"):
        print(f"noosphere-router: роль {role!r} без channel/model — пропущена, "
              "работает лестница тиров", file=sys.stderr)
        return []
    chain = [_role_entry(raw)]
    for item in (raw.get("failover") or []):
        if isinstance(item, str):
            item = {"channel": item}
        if isinstance(item, dict) and item.get("channel") and item.get("model"):
            chain.append(_role_entry(item))
        else:
            print(f"noosphere-router: запасной маршрут роли {role!r} без "
                  f"channel/model пропущен: {item!r}", file=sys.stderr)
    return chain

# Класс задачи → стартовый тир: «сколько эта РАБОТА стоит по своей природе»,
# а не «насколько она важна» — важная, но простая задача идёт дёшево.
CLASS_TIER = {
    "extract": "base", "summarize": "base", "classify": "base", "translate": "base",
    "research": "base", "explain": "base", "chat": "base",
    "code": "mid", "review": "mid", "refactor": "mid", "test": "mid",
    "debug": "deep", "arch": "deep", "orchestrate": "deep", "consistency": "deep",
    # деньги и юридические нюансы: дешёвая модель ошибается уверенно и неверно
    "legal": "max", "money": "max",
    # ночной судья успешности чатов: хорошая модель, но без лестницы (force_no_escalate)
    "audit": "deep",
}


_OVERRIDE_CACHE: dict[str, str] = {}
_OVERRIDE_LOADED = False
_MODEL_OVERRIDE_CACHE: dict[str, str] = {}
_MODEL_OVERRIDE_LOADED = False

# все модели лестницы — белый список для контрактов class_model_override
ALL_MODELS = {str(spec["model"]) for spec in TIERS.values()}


def reset_override_cache() -> None:
    global _OVERRIDE_CACHE, _OVERRIDE_LOADED, _MODEL_OVERRIDE_CACHE, _MODEL_OVERRIDE_LOADED
    _OVERRIDE_CACHE, _OVERRIDE_LOADED = {}, False
    _MODEL_OVERRIDE_CACHE, _MODEL_OVERRIDE_LOADED = {}, False


def model_override_for(task_class: str) -> str | None:
    """Модель под направление поверх модели тира (контракт A2, evidence аудита)."""
    global _MODEL_OVERRIDE_CACHE, _MODEL_OVERRIDE_LOADED
    if not _MODEL_OVERRIDE_LOADED:
        try:
            from . import db

            rows = db.get_db().execute(
                "SELECT class, model FROM class_model_override").fetchall()
            _MODEL_OVERRIDE_CACHE = {r["class"]: r["model"] for r in rows}
        except Exception:
            _MODEL_OVERRIDE_CACHE = {}
        _MODEL_OVERRIDE_LOADED = True
    return _MODEL_OVERRIDE_CACHE.get(task_class)


def _override_tier(task_class: str) -> str | None:
    """Ночной оптимизатор может переставить стартовый тир класса (контракт A2)."""
    global _OVERRIDE_CACHE, _OVERRIDE_LOADED
    if not _OVERRIDE_LOADED:
        try:
            from . import db

            rows = db.get_db().execute(
                "SELECT class, tier FROM class_tier_override").fetchall()
            _OVERRIDE_CACHE = {r["class"]: r["tier"] for r in rows}
        except Exception:
            _OVERRIDE_CACHE = {}
        _OVERRIDE_LOADED = True
    return _OVERRIDE_CACHE.get(task_class)


def start_tier(task_class: str) -> str:
    """Стартовый тир по классу задачи. Неизвестный класс → base + громкое
    предупреждение: опечатка молча уводила бы работу не туда ровно там, где
    цена ошибки высока (legal/money)."""
    override = _override_tier(task_class)
    if override:
        return override
    if task_class not in CLASS_TIER:
        print(f"noosphere-router: неизвестный класс {task_class!r} → взят base; известные: "
              f"{', '.join(sorted(CLASS_TIER))}", file=sys.stderr)
    return CLASS_TIER.get(task_class, "base")


def retryable(res: dict) -> bool:
    """Перебои сети/перегрузка — повторяем тот же тир, а не поднимаем модель."""
    return (not res.get("ok")) and (res.get("status") in (0, 429, 500, 502, 503, 504))


def passes(res: dict, check_json: bool, min_chars: int, must: str | None) -> tuple[bool, str]:
    """Прошёл ли ответ проверки приёмки. ok=False здесь — НЕ «транспорт молчал»,
    а «ответ есть, но он не годится» (обрыв, короткий, нет подстроки, не JSON)."""
    if not res.get("ok"):
        return False, res.get("err", "нет ответа")
    if res.get("finish") == "length":
        return False, "обрыв по max_tokens"
    t = res.get("text", "")
    if min_chars and len(t) < min_chars:
        return False, f"короче {min_chars} символов"
    if must and must not in t:
        return False, f"нет подстроки {must!r}"
    if check_json:
        s = t.strip()
        if s.startswith("```"):
            s = s.strip("`")
            s = s[s.find("{"):s.rfind("}") + 1] if "{" in s else s
        try:
            json.loads(s)
        except Exception:
            return False, "не разобрался как JSON"
    return True, ""
