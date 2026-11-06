"""voice.py — синтез речи для голосового режима «Разговор» (TTS-прокси).

Контур CT106 (192.168.0.22), замеры 01.11.2026:
  • ESpeech :8897 — клон голоса Армана: 3 зн → 2.26 с, 48 зн → 3.4 с (dur 3.72),
    nfe=8 → 1.29 с, nfe=12 → 1.93 с, nfe=16 → 2.2-3.4 с. Первый вызов холодный ~5 с.
  • Silero v4_ru :8898 — быстрый (159 мс), чужой голос: фолбэк, когда клон
    недоступен или не ответил в срок. Голос Армана важнее скорости, но молчание
    в разговоре хуже чужого голоса.
  • F5 не используем: 9.6 с на фразу — это уже не разговор, а пауза.

Оба сервиса — обычный http.server: POST JSON → audio/wav.

Кэш: sha256(text|voice|nfe|speed|engine) → data_dir/tts_cache/<key>.wav.
Разговор повторяет короткие фразы («да», «сейчас», «проверка озвучки») десятками
в день; повтор должен стоить <50 мс и не греть GPU на CT106. Потолок кэша —
по числу файлов и по байтам, вытеснение по mtime (LRU).

Контракт с вызывающим (main.py): synth() не бросает исключения на сбои движков,
а возвращает (wav|None, meta); meta = {engine, ms, cached, err}. Телеметрию пишет
main — там уже есть _tts_telemetry по образцу _stt_telemetry.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import pool
from .config import load_config

MAX_TEXT = 600              # потолок фразы: длинное в разговоре не дослушивают
TIMEOUT_ESPEECH = 30.0      # холодный первый вызов ~5 с, очередь GPU — с запасом
TIMEOUT_SILERO = 12.0
CACHE_MAX_FILES = 300
CACHE_MAX_BYTES = 64 * 1024 * 1024
ENGINES = ("auto", "espeech", "silero")


def cache_dir() -> Path:
    p = Path(load_config().data_dir) / "tts_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_key(text: str, voice: str, nfe_step: int, speed: float, engine: str) -> str:
    """Ключ кэша: sha256(text+voice+nfe). speed и engine добавлены, чтобы не
    отдать клон голоса Армана на запрос engine=silero и не склеить разные темпы."""
    raw = f"{engine}|{voice}|{nfe_step}|{speed}|{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    return cache_dir() / f"{key[:40]}.wav"


def cache_get(key: str) -> bytes | None:
    try:
        p = _cache_path(key)
        if not p.exists():
            return None
        data = p.read_bytes()
        if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return None
        os.utime(p, None)       # свежесть = порядок вытеснения (LRU по mtime)
        return data
    except OSError:
        return None


def cache_put(key: str, data: bytes) -> None:
    if not data:
        return
    try:
        p = _cache_path(key)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
    except OSError as e:
        print(f"tts cache write failed: {e!r}")
        return
    _prune()


def _prune() -> None:
    """Потолок кэша: число файлов и суммарные байты (data_dir не должен пухнуть).
    Вызывается из потока после записи, поэтому лишние stat не на горячем пути."""
    try:
        files = []
        total = 0
        for p in cache_dir().glob("*.wav"):
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
            total += st.st_size
        if len(files) <= CACHE_MAX_FILES and total <= CACHE_MAX_BYTES:
            return
        files.sort()
        while files and (len(files) > CACHE_MAX_FILES or total > CACHE_MAX_BYTES):
            _, size, p = files.pop(0)   # старые первыми
            try:
                p.unlink()
            except OSError:
                pass
            total -= size
    except OSError as e:
        print(f"tts cache prune failed: {e!r}")


def cache_stats() -> dict:
    files = 0
    total = 0
    try:
        for p in cache_dir().glob("*.wav"):
            try:
                total += p.stat().st_size
                files += 1
            except OSError:
                continue
    except OSError:
        pass
    return {"files": files, "bytes": total,
            "max_files": CACHE_MAX_FILES, "max_bytes": CACHE_MAX_BYTES}


def _root(url: str) -> str:
    s = urlsplit(url)
    return f"{s.scheme or 'http'}://{s.netloc}/" if s.netloc else url


async def synth(text: str, voice: str = "arman", speed: float = 0.95,
                nfe_step: int = 16, engine: str = "auto") -> tuple[bytes | None, dict]:
    """Озвучить text. Возвращает (wav|None, meta). Порядок движков:
    auto/espeech → ESpeech (клон голоса), при ошибке или таймауте → Silero;
    silero → сразу Silero. Пустой текст — ошибка вызывающего (main отдаёт 422)."""
    meta = {"engine": "", "ms": 0, "cached": False, "fallback": False, "err": ""}
    text = (text or "").strip()
    if not text:
        meta["err"] = "пустой текст"
        return None, meta
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]
    speed = min(1.5, max(0.5, float(speed or 0.95)))
    try:
        nfe_step = min(32, max(4, int(nfe_step or 16)))
    except (TypeError, ValueError):
        nfe_step = 16
    voice = (voice or "arman").strip() or "arman"

    t0 = time.time()
    key = cache_key(text, voice, nfe_step, speed, engine)
    hit = await asyncio.to_thread(cache_get, key)
    if hit:
        meta.update(engine="cache", ms=int((time.time() - t0) * 1000),
                    cached=True, err="")
        return hit, meta

    cfg = load_config().tools
    plan = ["silero"] if engine == "silero" else ["espeech", "silero"]
    errs: list[str] = []
    for name in plan:
        if name == "silero":
            url, timeout = cfg.tts_fallback_url, TIMEOUT_SILERO
            payload = {"text": text}
        else:
            url, timeout = cfg.tts_url, TIMEOUT_ESPEECH
            payload = {"text": text, "voice": voice,
                       "speed": speed, "nfe_step": nfe_step}
        try:
            r = await pool.CLIENT.post(url, json=payload, timeout=timeout)
            if r.status_code != 200:
                errs.append(f"{name} http {r.status_code}")
                continue
            wav = r.content or b""
            if len(wav) < 1024 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
                errs.append(f"{name}: не wav ({len(wav)} б)")
                continue
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as e:
            errs.append(f"{name}: {e!r}"[:160])
            continue
        meta.update(engine=name, ms=int((time.time() - t0) * 1000), err="",
                    fallback=name != plan[0])
        # В кэш кладём только ответ основного движка: иначе фолбэк на чужой голос
        # осел бы под ключом клона и пережил восстановление ESpeech.
        if name == plan[0]:
            await asyncio.to_thread(cache_put, key, wav)
        return wav, meta

    meta.update(engine="", ms=int((time.time() - t0) * 1000),
                err=("; ".join(errs) or "нет движков")[:400])
    return None, meta


async def health() -> dict:
    """Агрегированный статус обоих движков. Проверяем доступность корня сервиса
    (любой HTTP-ответ = процесс слушает), а не синтез: health не должен греть GPU."""
    cfg = load_config().tools
    out: dict = {"ok": False, "max_chars": MAX_TEXT, "engines": {}}
    for name, url in (("espeech", cfg.tts_url), ("silero", cfg.tts_fallback_url)):
        t0 = time.time()
        up, code, err = False, 0, ""
        try:
            r = await pool.CLIENT.get(_root(url), timeout=3)
            up, code = True, r.status_code
        except Exception as e:      # сеть/таймаут/битый URL — движок «лежит»
            err = repr(e)[:160]
        out["engines"][name] = {"url": url, "up": up, "http": code,
                                "ms": int((time.time() - t0) * 1000), "err": err}
    out["ok"] = any(e["up"] for e in out["engines"].values())
    out["cache"] = await asyncio.to_thread(cache_stats)
    return out
