"""Наблюдатель контура: опрос /v1/alerts → системные уведомления.

Что уведомляем (заказ владельца 2026-10-11): **crit-алерты**. Заявки A3 убраны
решением владельца 2026-10-17 — механизм approvals удалён с сервера.
Каждый id уведомляется один раз — состояние в
`%LOCALAPPDATA%\\Noosphere\\seen.json`, поэтому перезапуск окна не повторяет тосты.

Два предохранителя от шторма:
  * первый запуск не вываливает историю — только одна сводка о том, что уже висит;
  * обрыв связи даёт уведомление на ПЕРЕХОД (ok→broken, broken→ok), а не на каждый опрос.

HTTP подменяем через `fetch` — selftest гоняет логику на фикстурах без сети.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable

from . import appconfig, applog, httpapi
from .notify import Dispatcher, Notice

CRIT = "crit"
SEEN_LIMIT = 500
HEARTBEAT_EVERY = 20          # опросов; ~10 минут при интервале 30 с

ALERTS_PATH = "/v1/alerts"


def alert_notice(row: dict) -> Notice:
    kind = row.get("kind") or "alert"
    return Notice(kind="crit", title=f"Ноосфера · crit {kind}",
                  text=row.get("text") or "(без текста)", action="alerts")


def startup_summary(crits: int) -> Notice | None:
    """Сводка о том, что уже висит в контуре (вместо вываливания истории)."""
    if not crits:
        return None
    return Notice(kind="summary", title="Ноосфера · что ждёт тебя",
                  text=f"{crits} crit-алертов не закрыты. Открой окно — шторка справа.",
                  action="alerts")


class Watcher:
    def __init__(self, endpoint: Callable[[], tuple[str, str]], dispatch,
                 seen_path=None, clock: Callable[[], float] = time.time,
                 fetch: Callable[..., httpapi.Reply] | None = None,
                 interval: float = appconfig.POLL_INTERVAL) -> None:
        self._endpoint = endpoint
        self._sink = dispatch
        self._seen_path = seen_path or appconfig.seen_path()
        self._clock = clock
        self._fetch = fetch or (lambda server, path, token: httpapi.request(server, path, token))
        self.interval = interval
        self.state = self._load()
        self.polls = 0

    # ── состояние «кто уже уведомлён» ──
    def _load(self) -> dict:
        try:
            raw = json.loads(self._seen_path.read_text(encoding="utf-8"))
            state = {"alerts": [str(i) for i in raw.get("alerts", [])],
                     "broken": bool(raw.get("broken", False)),
                     "first_run": False}
            return state
        except (OSError, ValueError, AttributeError):
            return {"alerts": [], "broken": False, "first_run": True}

    def _save(self) -> None:
        try:
            self._seen_path.write_text(
                json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            applog.warn("seen state not saved", error=str(exc))

    def reset(self) -> None:
        self.state = {"alerts": [], "broken": False, "first_run": True}
        try:
            self._seen_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _diff(self, key: str, rows: list[dict]) -> list[dict]:
        """Новые id и отметка всех текущих как увиденных."""
        seen = set(self.state.get(key, []))
        current = [str(r.get("id")) for r in rows if r.get("id") is not None]
        fresh = [r for r in rows if str(r.get("id")) not in seen]
        self.state[key] = list(dict.fromkeys(list(seen) + current))[-SEEN_LIMIT:]
        return fresh

    # ── один опрос ──
    def poll_once(self) -> dict:
        server, token = self._endpoint()
        out = {"server": server, "alerts_new": 0, "outcomes": [],
               "error": "", "active_crits": 0,
               "first_run": self.state["first_run"]}
        if not server:
            out["error"] = "сервер не выбран"
            return out

        alerts_reply = self._fetch(server, ALERTS_PATH, token)
        if not alerts_reply.ok:
            out["error"] = alerts_reply.error
            out["outcomes"].append(self._transition(True))
            return out
        out["outcomes"].append(self._transition(False))

        alerts = [r for r in (alerts_reply.data or []) if isinstance(r, dict)]
        crits = [r for r in alerts if r.get("severity") == CRIT]
        out["active_crits"] = len(crits)

        fresh_crits = self._diff("alerts", crits)
        out["alerts_new"] = len(fresh_crits)

        if out["first_run"]:
            notice = startup_summary(len(crits))
            if notice:
                out["outcomes"].append(self.dispatch(notice))
        else:
            for row in fresh_crits:
                out["outcomes"].append(self.dispatch(alert_notice(row)))

        self.state["first_run"] = False
        self._save()
        return out

    def _transition(self, broken: bool) -> str:
        """Уведомление только на смене состояния связи."""
        was = bool(self.state.get("broken"))
        self.state["broken"] = broken
        if broken and not was:
            return self.dispatch(Notice(
                kind="info", title="Ноосфера · нет связи с сервером",
                text="Mac mini :8300 не отвечает. Окно показывает диагностику, "
                     "опрос продолжится сам.", action=""))
        if not broken and was:
            return self.dispatch(Notice(
                kind="info", title="Ноосфера · связь восстановлена",
                text="Сервер снова отвечает, опрос контура продолжен.", action=""))
        return ""

    def dispatch(self, notice: Notice) -> str:
        """Отдаёт уведомление наружу: Dispatcher с капом или простая функция.

        Исход всегда строкой (sent/summary/suppressed/send-failed), чтобы журнал
        и самопроверка читали одинаковые значения независимо от приёмника.
        """
        if not notice:
            return ""
        try:
            if isinstance(self._sink, Dispatcher):
                return self._sink.push(notice)
            return "sent" if int(self._sink(notice)) == 1 else "send-failed"
        except Exception as exc:
            applog.error("dispatch failed", error=f"{type(exc).__name__}: {exc}")
            return "send-failed"

    # ── фоновый цикл ──
    def run(self, stop: threading.Event) -> None:
        applog.info("watcher started", interval=self.interval)
        while not stop.is_set():
            self.polls += 1
            try:
                out = self.poll_once()
            except Exception as exc:
                applog.error("watcher poll crashed", error=f"{type(exc).__name__}: {exc}")
                out = {"error": str(exc)}
            noisy = (out.get("error") or out.get("alerts_new")
                     or any(o for o in out.get("outcomes", []) if o))
            if noisy or self.polls % HEARTBEAT_EVERY == 1:
                applog.info("poll", server=out.get("server", ""), polls=self.polls,
                            crits=out.get("active_crits", 0),
                            alerts_new=out.get("alerts_new", 0),
                            error=out.get("error", ""))
            if stop.wait(self.interval):
                break
        applog.info("watcher stopped", polls=self.polls)
