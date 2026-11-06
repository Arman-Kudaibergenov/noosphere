"""push.py — Web Push (VAPID) для PWA Ноосферы.

Зачем отдельный модуль: клиентский опрос живёт только пока вкладка жива.
Телефон в кармане, PWA свёрнуто — и «прогон закончился» узнаётся через час.
Web Push от сервера — единственный канал, который будит телефон сам.

Как устроено:
    * ключи VAPID генерятся один раз, лежат в <data_dir>/push_vapid.json (600);
    * подписки — таблица push_subs (endpoint — первичный ключ, ключи p256dh/auth);
    * notify() уходит в отдельном потоке: ход не должен ждать Google/FCM;
    * 404/410 от толкателя = подписка мертва, удаляем без вопросов.

Без pywebpush сервер работает как раньше: enabled() -> False, notify() — no-op.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .config import load_config
from .db import get_db, now_iso

log = logging.getLogger(__name__)

try:  # необязательная зависимость: остальной сервер живёт и без неё
    from pywebpush import WebPushException, webpush
    _HAVE_PYWEBPUSH = True
except Exception:  # pragma: no cover - окружение без пакета
    webpush = None
    WebPushException = Exception
    _HAVE_PYWEBPUSH = False

_VAPID_SUB = "mailto:arman@localhost"   # требование спеки: контакт в claims
_LOCK = threading.Lock()


def enabled() -> bool:
    return _HAVE_PYWEBPUSH


# ------------------------------------------------------------------ ключи VAPID
def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _key_file():
    return load_config().data_dir / "push_vapid.json"


def _load_keys() -> dict:
    """Ключи VAPID: создать при первом обращении, дальше только читать.

    Приватный ключ — PEM в PKCS8. Публичный — base64url несжатой точки P-256,
    ровно в том виде, в каком его ждёт PushManager.subscribe(applicationServerKey).
    """
    path = _key_file()
    with _LOCK:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("private_pem") and data.get("public_key"):
                    return data
            except Exception:
                log.warning("push_vapid.json битый — перегенерирую ключи")
        priv = ec.generate_private_key(ec.SECP256R1())
        pem = priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()).decode()
        pub = priv.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint)
        data = {"private_pem": pem, "public_key": _b64u(pub),
                "created_at": now_iso()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return data


def public_key() -> str:
    """Публичный ключ для клиента (applicationServerKey)."""
    return _load_keys()["public_key"]


# --------------------------------------------------------------- подписки
def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_subs (
            endpoint   TEXT PRIMARY KEY,
            p256dh     TEXT NOT NULL,
            auth       TEXT NOT NULL,
            ua         TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            last_ok    TEXT,
            fails      INTEGER DEFAULT 0
        )""")
    conn.commit()


def save_subscription(endpoint: str, p256dh: str, auth: str, ua: str = "") -> None:
    conn = get_db()
    _ensure_table(conn)
    conn.execute(
        """INSERT INTO push_subs(endpoint,p256dh,auth,ua,created_at,fails)
           VALUES(?,?,?,?,?,0)
           ON CONFLICT(endpoint) DO UPDATE SET
             p256dh=excluded.p256dh, auth=excluded.auth, ua=excluded.ua""",
        (endpoint, p256dh, auth, (ua or "")[:200], now_iso()))
    conn.commit()


def delete_subscription(endpoint: str) -> None:
    conn = get_db()
    _ensure_table(conn)
    conn.execute("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))
    conn.commit()


def subscriptions() -> list[dict]:
    conn = get_db()
    _ensure_table(conn)
    return [dict(r) for r in conn.execute(
        "SELECT endpoint,p256dh,auth,fails FROM push_subs").fetchall()]


def count() -> int:
    return len(subscriptions())


# --------------------------------------------------------------- отправка
def _payload(title: str, body: str, sid: str | None, tag: str) -> str:
    return json.dumps({
        "title": title,
        "body": (body or "").strip()[:300],
        "sid": sid or "",
        "tag": tag,
        "url": ("/?sid=" + sid) if sid else "/",
    }, ensure_ascii=False)


def _vapid_key() -> str:
    """Ключ для pywebpush в том виде, в каком его ждёт py_vapid.

    Грабли (найдено 2026-11-01): webpush(vapid_private_key=<PEM>) не работает —
    py_vapid.Vapid.from_string срезает переводы строк и декодирует остаток как
    DER, то есть PEM-заголовки ломают разбор («ASN.1 parsing error: invalid
    length»), и пуш не уходил никогда. Понимает он только base64url-DER (или
    raw 32 байта). Поэтому PEM конвертим в DER сами, а если в файле уже лежит
    base64url-DER — отдаём как есть.
    """
    pem = _load_keys()["private_pem"]
    if "BEGIN" not in pem:
        return "".join(pem.split())
    key = serialization.load_pem_private_key(pem.encode(), None)
    der = key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    return _b64u(der)


def _send_one(sub: dict, payload: str) -> None:
    info = {"endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
    key = _vapid_key()
    # TTL сутки: телефон мог быть выключен — уведомление всё равно дождётся
    webpush(subscription_info=info, data=payload,
            vapid_private_key=key, vapid_claims={"sub": _VAPID_SUB},
            ttl=86400, timeout=10)


def notify(title: str, body: str = "", sid: str | None = None,
           tag: str = "noosphere") -> None:
    """Разослать пуш всем подпискам — из фонового потока, не блокируя ход."""
    if not _HAVE_PYWEBPUSH:
        return

    def work() -> None:
        payload = _payload(title, body, sid, tag)
        dead: list[str] = []
        for sub in subscriptions():
            try:
                _send_one(sub, payload)
            except WebPushException as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code in (404, 410):
                    dead.append(sub["endpoint"])
                else:
                    log.info("push не ушёл (%s): %s", code, e)
            except Exception as e:  # сеть, таймаут, кривой ключ
                log.info("push не ушёл: %s", e)
        for ep in dead:
            delete_subscription(ep)
            log.info("push: подписка мертва, удалил (%s)", ep[:40])

    threading.Thread(target=work, name="push", daemon=True).start()


def notify_turn(sid: str, inp: dict, res: dict, kind: str = "inline") -> None:
    """Уведомление о завершённом ходе. Вызывается из main._run_turn."""
    if not _HAVE_PYWEBPUSH:
        return
    title = ""
    try:
        from . import sessions as _sessions
        row = _sessions.get_session(sid) or {}
        title = (row.get("title") or "").strip()
    except Exception:
        title = ""
    if not title:
        title = "Сессия " + str(sid)[:8]
    ok = bool(res.get("ok", True)) and not res.get("err")
    text = (res.get("text") or "").strip().replace("\n", " ")
    if not ok:
        text = (res.get("err") or text or "ход упал")
    head = title + (" · ошибка" if not ok else " · готово")
    notify(head, text or "прогон завершён", sid=sid, tag="turn-" + str(sid)[:12])
