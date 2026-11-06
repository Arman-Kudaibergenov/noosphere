"""Событие → тост: полезная нагрузка балуна и кап от шторма уведомлений.

Лимиты Windows жёсткие: `szInfoTitle` ≤ 64 символа, `szInfo` ≤ 256 — обрезаем
здесь, иначе Shell_NotifyIconW молча отбрасывает балун.

Кап — прямой урок аудита r3 (эскалация без капа, ~26 сообщений за ночь):
не больше `LIMIT` тостов за `WINDOW` секунд, затем один сводный и тишина до
окончания окна. Наблюдатель при этом продолжает копить счётчик.

Модуль не знает про Win32: `send(payload) -> int` подставляется снаружи
(в бою — трей, в selftest — регистратор вызовов).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import applog

NIIF_INFO = 0x1
NIIF_WARNING = 0x2
NIIF_ERROR = 0x3

TITLE_LIMIT = 63   # 64 с завершающим NUL
TEXT_LIMIT = 255   # 256 с завершающим NUL

WINDOW = 1800.0    # 30 минут
LIMIT = 5          # тостов на окно

KIND_FLAGS = {"crit": NIIF_ERROR, "info": NIIF_INFO, "summary": NIIF_INFO}


@dataclass(frozen=True)
class Notice:
    kind: str            # crit | info | summary
    title: str
    text: str
    action: str = ""     # куда вести по клику: alerts | ""


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def build_payload(notice: Notice) -> dict[str, object]:
    """Нагрузка для Shell_NotifyIconW: обрезанные поля и флаг важности."""
    return {
        "title": clip(notice.title, TITLE_LIMIT),
        "text": clip(notice.text, TEXT_LIMIT),
        "flags": KIND_FLAGS.get(notice.kind, NIIF_INFO),
        "tip": clip(notice.title, TITLE_LIMIT),
        "action": notice.action,
        "kind": notice.kind,
    }


def summary_notice(count: int) -> Notice:
    return Notice(kind="summary", title="Ноосфера · уведомления свёрнуты",
                  text=f"Событий ещё {count}, но лимит тостов на 30 минут исчерпан. "
                       f"Открой окно — шторка показывает всё.",
                  action="alerts")


class Cap:
    """Сколько тостов уже ушло и не пора ли сворачивать их в сводку."""

    def __init__(self, window: float = WINDOW, limit: int = LIMIT) -> None:
        self.window = window
        self.limit = limit
        self.sent: list[float] = []
        self.suppressed = 0
        self.summary_sent = False

    def prune(self, now: float) -> None:
        fresh = [ts for ts in self.sent if now - ts < self.window]
        if len(fresh) != len(self.sent):
            self.sent = fresh
        if not self.sent and (self.summary_sent or self.suppressed):
            self.summary_sent = False
            self.suppressed = 0

    def admit(self, now: float) -> str:
        """`send` — обычный тост, `summary` — сводный, `suppress` — молчим."""
        self.prune(now)
        if len(self.sent) < self.limit:
            return "send"
        self.suppressed += 1
        return "suppress" if self.summary_sent else "summary"

    def record(self, now: float) -> None:
        self.sent.append(now)


class Dispatcher:
    """Превращает Notice в отправленный тост с учётом капа."""

    def __init__(self, send, clock=time.time, cap: Cap | None = None) -> None:
        self._send = send
        self._clock = clock
        self.cap = cap or Cap()

    def push(self, notice: Notice) -> str:
        """sent | summary | suppressed | send-failed"""
        now = self._clock()
        verdict = self.cap.admit(now)
        if verdict == "suppress":
            applog.info("notice suppressed", kind=notice.kind, title=notice.title,
                        suppressed=self.cap.suppressed)
            return "suppressed"
        payload = build_payload(notice if verdict == "send"
                                else summary_notice(self.cap.suppressed))
        try:
            rc = int(self._send(payload))
        except Exception as exc:            # сбой трея не должен ронять наблюдателя
            applog.error("toast failed", error=f"{type(exc).__name__}: {exc}")
            return "send-failed"
        if rc != 1:
            applog.error("toast refused", rc=rc, kind=payload["kind"])
            return "send-failed"
        if verdict == "send":
            self.cap.record(now)
        else:
            # сводка не ест слот лимита: она и есть «последний тост окна»
            self.cap.summary_sent = True
        applog.info("toast sent", kind=payload["kind"], verdict=verdict,
                    title=payload["title"], rc=rc)
        return "sent" if verdict == "send" else "summary"
