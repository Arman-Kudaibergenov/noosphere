"""trajectory.py — траектория хода: каждый вызов модели и каждый вызов инструмента по шагам.

Контракт:
    rec, token = begin(session_id)       # в начале хода (_run_turn), контекст-переменная
    bind_turn(turn_no)                   # номер хода известен после sessions.append_turn
    note_model(rec_turn)                 # из telemetry.record_turn — КАЖДЫЙ вызов модели
    wrap_executor(fn, ctx) -> fn         # исполнитель инструментов хода с замером шага
    reset(token); rec.flush()            # в finally хода: шаги — одной транзакцией в turn_steps
    steps_for(session_id, turn_no)       # чтение для GET .../trajectory

Порт модуля из репозитория GitLab Биокарда (проект 534) 30.10.2026 без изменений:
решение владельца «добавь траекторию ко мне». Модель зовётся из многих мест
(лестница тиров, wrap-up, судьи), но телеметрию пишет всегда одна функция —
telemetry.record_turn; туда и встроен хук, цикл pool.py не тронут. Вложенность —
через контекст-переменную текущего шага-инструмента: вызов модели, случившийся
внутри инструмента, получает parent_seq этого шага.

Секреты: аргументы — тем же замаскированным резюме, что tool_audit (tools._args_summary +
_mask_secrets), плюс вырезаются значения reveal_secrets хода. Вывод инструмента НЕ
пишется — только длина. Сбой записи траектории ход не роняет (это журнал, а не данные).
"""
from __future__ import annotations

import asyncio
import contextvars
import sys
import threading
import time

from .db import get_db, now_iso

MAX_STEPS = 400          # потолок шагов на ход: зациклившийся ход не раздувает таблицу
SUMMARY_MAX = 300
ERR_MAX = 300
# Каналы зовутся без стрима (pool.py, "stream": False): время до первого токена
# не измерить, ttft_ms = NULL.
NO_STREAM = "нет стрима"

_REC: contextvars.ContextVar["Recorder | None"] = contextvars.ContextVar("trajectory_rec",
                                                                         default=None)
_PARENT: contextvars.ContextVar[int | None] = contextvars.ContextVar("trajectory_parent",
                                                                     default=None)

# Живая полоса «в работе» (окно v5): последний шаг каждого идущего хода держится
# в памяти, а не в БД — запись в turn_steps по-прежнему одной транзакцией в flush.
LIVE: dict[str, dict] = {}
_LIVE_LOCK = threading.Lock()
_LIVE_LABEL_MAX = 120


def _live_note(rec: "Recorder", step: dict) -> None:
    if step.get("kind") == "tool":
        label = f"{step.get('tool') or '?'} {step.get('args_summary') or ''}".strip()
    else:
        label = f"модель · {step.get('model') or step.get('channel') or '?'}"
    with _LIVE_LOCK:
        LIVE[rec.session_id] = {"steps": len(rec.steps),
                                "label": label[:_LIVE_LABEL_MAX], "t0": rec.t0}


def live_clear(session_id: str) -> None:
    with _LIVE_LOCK:
        LIVE.pop(session_id, None)


def live_snapshot() -> list[dict]:
    now = time.time()
    with _LIVE_LOCK:
        return [{"sid": sid, "steps": v["steps"], "label": v["label"],
                 "elapsed_s": max(0, int(now - v["t0"]))} for sid, v in LIVE.items()]


def _iso_ms(t: float) -> str:
    import datetime as dt
    return (dt.datetime.fromtimestamp(t, dt.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


class Recorder:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.turn_no: int | None = None
        self.t0 = time.time()
        self.steps: list[dict] = []
        self.mask: list[str] = []
        self.closed = False
        self.dropped = 0
        self._lock = threading.Lock()
        self._depth: dict[int, int] = {}
        self.ctxs: list[dict] = []   # tool_ctx хода: reveal_secrets растут по ходу (vault)

    def _clean(self, s: str, limit: int) -> str:
        s = s or ""
        for sec in self.mask:
            if sec:
                s = s.replace(sec, "***")
        return s[:limit]

    def add(self, step: dict, started: float) -> dict | None:
        with self._lock:
            if self.closed:
                return None
            if len(self.steps) >= MAX_STEPS:
                self.dropped += 1
                return None
            parent = _PARENT.get()
            step["seq"] = len(self.steps) + 1
            step["parent_seq"] = parent
            step["depth"] = (self._depth.get(parent, 0) + 1) if parent else 0
            step["started_at"] = _iso_ms(started)
            step["offset_ms"] = max(0, int((started - self.t0) * 1000))
            self._depth[step["seq"]] = step["depth"]
            self.steps.append(step)
            try:
                _live_note(self, step)
            except Exception:                       # живой индикатор не роняет ход
                pass
            return step

    def flush(self) -> int:
        """Шаги — в turn_steps одной транзакцией. Без номера хода (ход упал до
        append_turn) писать не к чему — 0. Живой индикатор гасим всегда."""
        live_clear(self.session_id)
        with self._lock:
            self.closed = True
            steps = list(self.steps)
        if self.turn_no is None or not steps:
            return 0
        # маска — на момент записи: секрет, раскрытый позже, вычищается и из ранних шагов
        for c in self.ctxs:
            self.mask = list({*self.mask, *[m for m in c.get("reveal_secrets") or [] if m]})
        for st in steps:
            for k in ("why", "err", "args_summary"):
                if st.get(k):
                    st[k] = self._clean(st[k], SUMMARY_MAX if k != "err" else ERR_MAX)
        try:
            db = get_db()
            ts = now_iso()
            cols = ("session_id, turn_no, seq, parent_seq, depth, kind, started_at, offset_ms, ms,"
                    " ok, status, channel, model, tier, task_class, prompt_tokens,"
                    " completion_tokens, reasoning_tokens, cached_tokens, ttft_ms, escalated,"
                    " why, finish, err, tool, args_summary, rc, out_len, created_at")
            params: list = []
            for s in steps:
                params += [self.session_id, self.turn_no, s["seq"], s["parent_seq"], s["depth"],
                           s["kind"], s["started_at"], s["offset_ms"], s.get("ms", 0),
                           int(bool(s.get("ok"))), s.get("status", ""), s.get("channel"),
                           s.get("model"), s.get("tier"), s.get("task_class"),
                           s.get("prompt_tokens", 0), s.get("completion_tokens", 0),
                           s.get("reasoning_tokens", 0), s.get("cached_tokens", 0),
                           s.get("ttft_ms"), int(bool(s.get("escalated"))), s.get("why"),
                           s.get("finish"), s.get("err"), s.get("tool"), s.get("args_summary"),
                           s.get("rc"), s.get("out_len"), ts]
            row = "(" + ",".join(["?"] * 29) + ")"
            # одна вставка на ход: один круг до базы, а не 400
            db.execute(f"INSERT INTO turn_steps ({cols}) VALUES " + ",".join([row] * len(steps)),
                       tuple(params))
            if self.dropped:
                print(f"[trajectory] {self.session_id}#{self.turn_no}: шагов больше "
                      f"{MAX_STEPS}, {self.dropped} не записаны", file=sys.stderr)
            db.commit()
            return len(steps)
        except Exception as e:
            try:
                get_db().rollback()
            except Exception:
                pass
            print(f"[trajectory] запись шагов {self.session_id}#{self.turn_no} упала: {e!r}",
                  file=sys.stderr)
            return 0


def record_step(session_id: str, turn_no: int, step: dict) -> int:
    """Разовый шаг вне хода (стадия процесса без модели: клон, доставка). ms — длительность,
    начало шага — сейчас минус ms. Сбой записи не роняет вызывающего (как flush)."""
    rec = Recorder(session_id)
    rec.turn_no = turn_no
    ms = int(step.get("ms") or 0)
    rec.t0 = time.time() - ms / 1000
    rec.add(dict(step), rec.t0)
    return rec.flush()


def begin(session_id: str) -> tuple[Recorder, contextvars.Token]:
    rec = Recorder(session_id)
    return rec, _REC.set(rec)


def reset(token: contextvars.Token) -> None:
    _REC.reset(token)


def current() -> Recorder | None:
    return _REC.get()


def bind_turn(turn_no: int, mask: list[str] | None = None) -> None:
    rec = _REC.get()
    if rec is not None:
        rec.turn_no = turn_no
        rec.mask = [m for m in (mask or []) if m]


def note_model(t) -> None:
    """Хук telemetry.record_turn: один вызов модели — один шаг (t — telemetry.TurnRec)."""
    rec = _REC.get()
    if rec is None:
        return
    try:
        ms = int(t.ms or 0)
        if t.ok:
            status = "ok"
        elif t.answered:
            status = "fail"        # транспорт ответил, проверка не прошла
        else:
            status = "error"
        rec.add({"kind": "model", "ms": ms, "ok": bool(t.ok), "status": status,
                 "channel": t.channel, "model": t.model, "tier": t.tier_used,
                 "task_class": t.task_class, "prompt_tokens": int(t.prompt_tokens or 0),
                 "completion_tokens": int(t.completion_tokens or 0),
                 "reasoning_tokens": int(t.reasoning_tokens or 0),
                 "cached_tokens": int(t.cache_read_tokens or 0),
                 "ttft_ms": getattr(t, "ttft_ms", None), "escalated": bool(t.escalated),
                 "why": rec._clean(t.why, SUMMARY_MAX), "finish": t.finish or "",
                 "err": rec._clean(t.err, ERR_MAX)},
                time.time() - ms / 1000)
    except Exception as e:  # журнал не роняет ход
        print(f"[trajectory] note_model: {e!r}", file=sys.stderr)


def _tool_status(name: str, out: str, args_summary: str = "") -> tuple[str, int | None]:
    from . import tools
    rc = tools._infer_rc(name, out)
    head = out[:80]
    if any(out.startswith(p) for p in tools._REFUSE_PREFIXES):
        return "refuse", rc
    if tools.is_benign_rc(name, rc, args_summary):
        return "ok", rc        # grep rc=1 — «совпадений нет», не ошибка
    if (rc not in (None, 0) or " упал:" in head or "не уложилась" in head
            or head.lower().startswith(("ошибка", "error"))):
        return "error", rc
    return "ok", rc


def wrap_executor(fn, ctx: dict | None = None):
    """Исполнитель инструментов хода → тот же исполнитель с шагом траектории на вызов."""
    async def run(tc: dict) -> str:
        rec = _REC.get()
        if rec is None:
            return await fn(tc)
        from . import tools
        import json
        f = tc.get("function") or {}
        name = f.get("name", "?")
        try:
            args = json.loads(f.get("arguments") or "{}")
        except Exception:
            args = {}
        try:
            summary = tools._mask_secrets(
                tools._args_summary(name, args if isinstance(args, dict) else {}))
        except Exception:
            summary = ""
        if ctx is not None and not any(c is ctx for c in rec.ctxs):
            rec.ctxs.append(ctx)
        started = time.time()
        step = rec.add({"kind": "tool", "tool": name,
                        "args_summary": rec._clean(summary, SUMMARY_MAX),
                        "ok": False, "status": "running"}, started)
        token = _PARENT.set(step["seq"]) if step else None
        try:
            out = await fn(tc)
        except BaseException as e:
            if step:
                # отмена хода — «оборван», а не ошибка инструмента
                cancelled = isinstance(e, (asyncio.CancelledError, KeyboardInterrupt))
                step.update(ms=int((time.time() - started) * 1000),
                            status="running" if cancelled else "error",
                            err=None if cancelled else rec._clean(repr(e), ERR_MAX))
            raise
        finally:
            if token is not None:
                _PARENT.reset(token)
        if step:
            text = out if isinstance(out, str) else str(out)
            status, rc = _tool_status(name, text, summary)
            step.update(ms=int((time.time() - started) * 1000), status=status,
                        ok=status == "ok", rc=rc, out_len=len(text))
            if status != "ok":
                try:
                    head = tools._mask_secrets(text[:ERR_MAX * 2])
                except Exception:
                    head = "<вывод не показан: маскировка не удалась>"
                step["err"] = rec._clean(head, ERR_MAX)
        return out
    return run


def steps_for(session_id: str, turn_no: int) -> list[dict]:
    rows = get_db().execute(
        "SELECT seq, parent_seq, depth, kind, started_at, offset_ms, ms, ok, status, channel,"
        " model, tier, task_class, prompt_tokens, completion_tokens, reasoning_tokens,"
        " cached_tokens, ttft_ms, escalated, why, finish, err, tool, args_summary, rc, out_len"
        " FROM turn_steps WHERE session_id = ? AND turn_no = ? ORDER BY seq",
        (session_id, turn_no)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ok"] = bool(d["ok"])
        d["escalated"] = bool(d["escalated"])
        if d["kind"] == "model" and d["ttft_ms"] is None:
            d["ttft_note"] = NO_STREAM
        out.append(d)
    return out


def totals(steps: list[dict]) -> dict:
    """Итоги траектории. Время инструментов — только верхнего уровня (depth=0): вложенный
    шаг уже входит во время своего родителя, иначе сумма удвоится."""
    model = [s for s in steps if s["kind"] == "model"]
    tool = [s for s in steps if s["kind"] == "tool"]
    model_top = [s for s in model if not s["parent_seq"]]
    ttfts = [s["ttft_ms"] for s in model if s.get("ttft_ms") is not None]
    return {
        "steps": len(steps), "model_calls": len(model), "tool_calls": len(tool),
        "model_ms": sum(s["ms"] or 0 for s in model_top),
        "tool_ms": sum(s["ms"] or 0 for s in tool if not s["parent_seq"]),
        "prompt_tokens": sum(s["prompt_tokens"] or 0 for s in model),
        "completion_tokens": sum(s["completion_tokens"] or 0 for s in model),
        "reasoning_tokens": sum(s["reasoning_tokens"] or 0 for s in model),
        "cached_tokens": sum(s["cached_tokens"] or 0 for s in model),
        "errors": sum(1 for s in steps if s["status"] in ("error", "fail", "refuse")),
        "ttft_ms": ttfts[0] if ttfts else None,
        # не замерено — говорим почему, а не отдаём пустое значение
        "ttft_note": None if ttfts or not model else NO_STREAM,
        "span_ms": max((s["offset_ms"] + (s["ms"] or 0) for s in steps), default=0),
    }
