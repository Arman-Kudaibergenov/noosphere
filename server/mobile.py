"""mobile.py — мобильный доступ к Ноосфере по образцу Hermes Mobile.

Зачем отдельный раздел: телефон не должен получать главный bearer владельца.
Профиль подключения — это отдельная, отзываемая единица доступа: имя, адрес
сервера, режим авторизации, срок действия, опциональный PIN. Отзыв профиля =
один UPDATE, и все выданные им токены/сертификаты перестают работать сразу.

Правила, зафиксированные в коде:

* bearer-токен профиля — `nsm_…`, показывается владельцу ОДИН раз (в БД лежит
  только sha256 и «хвост» для карточки). В логи токен и PIN не попадают.
* basic-режим — логин/хеш пароля профиля (pbkdf2-sha256); наружу пароль тоже
  отдаётся один раз, при выдаче.
* mTLS — клиентский сертификат генерирует сервер: приватный ключ отдаётся
  клиенту один раз и НЕ хранится, в БД остаётся sha256-отпечаток DER и серийник
  для проверки предъявленного сертификата (`verify_fingerprint`).
* PIN профиля (опционально) проверяется сервером; сессия без подтверждённого PIN
  помечена pin_ok=0 и инструменты ей не открываются (см. `session_allows_tools`).
* SSH-туннель: `ssh -N -L <port>:127.0.0.1:<port> user@host`, где port — из
  конфига сервиса (`server.port`), не хардкод. QR рисует server/qr.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db
from .config import load_config

TOKEN_PREFIX = "nsm_"
TOKEN_HINT_LEN = 4
AUTH_MODES = ("bearer", "basic", "mtls")
PIN_MIN, PIN_MAX = 4, 12
_PBKDF2_ROUNDS = 120_000


class MobileError(Exception):
    """Отказ мобильного доступа с HTTP-кодом (эндпоинты транслируют как есть)."""

    def __init__(self, status: int, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra


# ─────────────────────────────────────────────────────────────── примитивы
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _kdf(secret: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"),
                               bytes.fromhex(salt_hex), _PBKDF2_ROUNDS).hex()


def _expired(iso: str) -> bool:
    """'' = бессрочно. Сравнение ISO-строк одного формата корректно."""
    if not iso:
        return False
    return iso < db.now_iso()


def _check_pin(pin: str) -> str:
    pin = (pin or "").strip()
    if not (PIN_MIN <= len(pin) <= PIN_MAX) or not pin.isdigit():
        raise MobileError(400, f"PIN — от {PIN_MIN} до {PIN_MAX} цифр")
    return pin


def _norm_addr(addr: str) -> str:
    addr = (addr or "").strip()
    for prefix in ("ssh://", "http://", "https://"):
        if addr.lower().startswith(prefix):
            addr = addr[len(prefix):]
    return addr.rstrip("/")


# ─────────────────────────────────────────────────────────────── профили
def _row(r) -> dict:
    return dict(r) if r is not None else {}


def _public(row: dict) -> dict:
    """Карточка профиля наружу: без хешей токена/PIN/пароля."""
    p = {
        "id": row.get("id"),
        "name": row.get("name", ""),
        "server_addr": row.get("server_addr", ""),
        "auth_mode": row.get("auth_mode", "bearer"),
        "expires_at": row.get("expires_at", ""),
        "revoked": bool(row.get("revoked")),
        "created_at": row.get("created_at", ""),
        "updated_at": row.get("updated_at", ""),
        "last_seen": row.get("last_seen", ""),
        "has_pin": bool(row.get("pin_hash")),
        "token_hint": row.get("token_hint", ""),
        "token_issued_at": row.get("token_issued_at", ""),
        "has_basic": bool(row.get("basic_hash")),
        "basic_login": row.get("basic_login", ""),
        "cert_fingerprint": row.get("cert_fingerprint", ""),
        "cert_serial": row.get("cert_serial", ""),
        "cert_expires_at": row.get("cert_expires_at", ""),
        "expired": _expired(row.get("expires_at", "")),
    }
    return p


def get(pid: int) -> dict:
    row = db.get_db().execute(
        "SELECT * FROM mobile_profiles WHERE id = ?", (int(pid),)).fetchone()
    if row is None:
        raise MobileError(404, "профиль не найден")
    return _row(row)


def list_profiles(include_revoked: bool = True) -> list[dict]:
    sql = "SELECT * FROM mobile_profiles"
    if not include_revoked:
        sql += " WHERE revoked = 0"
    rows = db.get_db().execute(sql + " ORDER BY revoked ASC, id DESC").fetchall()
    out = []
    for r in rows:
        card = _public(_row(r))
        card["active_sessions"] = _active_count(card["id"])
        out.append(card)
    return out


def _days(expires_in_days: int | None) -> str:
    cfg = load_config()
    days = int(expires_in_days if expires_in_days is not None
               else cfg.mobile.default_expires_days)
    if days <= 0:
        return ""                     # бессрочный профиль
    return _iso(_now() + timedelta(days=days))


def create(name: str, server_addr: str, auth_mode: str = "bearer",
           expires_in_days: int | None = None, pin: str = "",
           basic_login: str = "", basic_password: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise MobileError(400, "имя профиля обязательно")
    mode = (auth_mode or "bearer").strip().lower()
    if mode not in AUTH_MODES:
        raise MobileError(400, f"режим авторизации: {', '.join(AUTH_MODES)}")
    addr = _norm_addr(server_addr)
    if not addr:
        raise MobileError(400, "адрес сервера обязателен")
    expires_at = _days(expires_in_days)
    pin_hash = pin_salt = ""
    if pin:
        pin = _check_pin(pin)
        pin_salt = secrets.token_hex(16)
        pin_hash = _kdf(pin, pin_salt)
    basic_hash = basic_salt = ""
    if mode == "basic":
        if not basic_login or not basic_password:
            raise MobileError(400, "basic-режим требует логин и пароль")
        basic_salt = secrets.token_hex(16)
        basic_hash = _kdf(basic_password, basic_salt)
    ts = db.now_iso()
    cur = db.get_db().execute(
        "INSERT INTO mobile_profiles(name, server_addr, auth_mode, expires_at,"
        " basic_login, basic_hash, basic_salt, pin_hash, pin_salt,"
        " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (name, addr, mode, expires_at, (basic_login or "").strip(), basic_hash,
         basic_salt, pin_hash, pin_salt, ts, ts))
    db.get_db().commit()
    pid = int(cur.lastrowid)
    return _public(get(pid))


def update(pid: int, **fields) -> dict:
    row = get(pid)
    if row.get("revoked"):
        raise MobileError(409, "профиль отозван — правка запрещена")
    sets: list[str] = []
    args: list[Any] = []

    def put(col: str, val: Any) -> None:
        sets.append(f"{col} = ?")
        args.append(val)

    if fields.get("name") is not None:
        name = str(fields["name"]).strip()
        if not name:
            raise MobileError(400, "имя профиля не может быть пустым")
        put("name", name)
    if fields.get("server_addr") is not None:
        addr = _norm_addr(str(fields["server_addr"]))
        if not addr:
            raise MobileError(400, "адрес сервера не может быть пустым")
        put("server_addr", addr)
    if fields.get("auth_mode") is not None:
        mode = str(fields["auth_mode"]).strip().lower()
        if mode not in AUTH_MODES:
            raise MobileError(400, f"режим авторизации: {', '.join(AUTH_MODES)}")
        put("auth_mode", mode)
    if fields.get("expires_in_days") is not None:
        put("expires_at", _days(int(fields["expires_in_days"])))
    if "pin" in fields:
        pin = str(fields.get("pin") or "").strip()
        if pin == "":                       # снять PIN
            put("pin_hash", "")
            put("pin_salt", "")
        else:
            pin = _check_pin(pin)
            salt = secrets.token_hex(16)
            put("pin_hash", _kdf(pin, salt))
            put("pin_salt", salt)
    if fields.get("basic_login") is not None or fields.get("basic_password"):
        login = str(fields.get("basic_login") or row.get("basic_login") or "").strip()
        pwd = str(fields.get("basic_password") or "")
        if not login or not pwd:
            raise MobileError(400, "смена basic-доступа требует логин и пароль")
        salt = secrets.token_hex(16)
        put("basic_login", login)
        put("basic_hash", _kdf(pwd, salt))
        put("basic_salt", salt)
    if not sets:
        return _public(row)
    put("updated_at", db.now_iso())
    db.get_db().execute(
        f"UPDATE mobile_profiles SET {', '.join(sets)} WHERE id = ?", (*args, int(pid)))
    db.get_db().commit()
    return _public(get(pid))


def revoke(pid: int) -> dict:
    """Отзыв: токен/сертификат профиля гасятся, живые сессии закрываются."""
    row = get(pid)
    if row.get("revoked"):
        return _public(row)
    ts = db.now_iso()
    d = db.get_db()
    d.execute("UPDATE mobile_profiles SET revoked = 1, token_hash = '', token_hint = '',"
              " cert_fingerprint = '', basic_hash = '', pin_hash = '', updated_at = ?"
              " WHERE id = ?", (ts, int(pid)))
    closed = d.execute("UPDATE mobile_sessions SET closed = 1, closed_at = ?"
                       " WHERE profile_id = ? AND closed = 0", (ts, int(pid))).rowcount
    d.commit()
    card = _public(get(pid))
    card["closed_sessions"] = int(closed or 0)
    return card


# ─────────────────────────────────────────────────────────────── токены/basic
def login_basic(login: str, password: str) -> dict:
    """Вход «ссылка + логин» (окно v5): пара логин/пароль basic-профиля →
    свежий bearer nsm_. Ошибка одинакова для «нет такого логина» и «неверный
    пароль» — не подсказываем перебору. Проверка пароля — до выдачи, отзыв и
    срок профиля проверяет issue_token."""
    row = db.get_db().execute(
        "SELECT * FROM mobile_profiles WHERE basic_login=? AND auth_mode='basic'",
        ((login or "").strip(),)).fetchone()
    row = dict(row) if row else None
    ok = False
    if row and row.get("basic_hash"):
        ok = hmac.compare_digest(_kdf(password or "", row["basic_salt"]),
                                 row["basic_hash"])
    if not ok:
        raise MobileError(401, "неверный логин или пароль")
    return issue_token(int(row["id"]))


def issue_token(pid: int) -> dict:
    row = get(pid)
    if row.get("revoked"):
        raise MobileError(409, "профиль отозван")
    if _expired(row.get("expires_at", "")):
        raise MobileError(409, "срок действия профиля истёк")
    if row.get("auth_mode") == "mtls":
        raise MobileError(409, "профиль в режиме mTLS — токен не выдаётся")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    ts = db.now_iso()
    d = db.get_db()
    d.execute("UPDATE mobile_profiles SET token_hash = ?, token_hint = ?,"
              " token_issued_at = ?, updated_at = ? WHERE id = ?",
              (_sha256(token), token[-TOKEN_HINT_LEN:], ts, ts, int(pid)))
    d.commit()
    cfg = load_config()
    return {
        "profile_id": int(pid),
        "token": token,                     # ОДИН раз, в БД только хеш
        "token_hint": token[-TOKEN_HINT_LEN:],
        "issued_at": ts,
        "expires_at": row.get("expires_at", ""),
        "warning": "Токен показывается один раз. Сохраните его на телефоне — "
                   "повторно сервер его не выдаст, только новый.",
        # ссылка для телефона: loopback через SSH-туннель, порт из конфига
        "login_url": f"http://{cfg.mobile.loopback_host}:{int(cfg.server.port)}/",
    }


def token_profile(token: str) -> dict | None:
    """Профиль по предъявленному bearer-токену; None — токен недействителен."""
    token = (token or "").strip()
    if not token.startswith(TOKEN_PREFIX):
        return None
    row = db.get_db().execute(
        "SELECT * FROM mobile_profiles WHERE token_hash = ?", (_sha256(token),)).fetchone()
    if row is None:
        return None
    prof = _row(row)
    if prof.get("revoked") or _expired(prof.get("expires_at", "")):
        return None
    return prof


def check_basic(login: str, password: str) -> dict | None:
    """Проверка basic-пары профиля (для режима basic)."""
    login = (login or "").strip()
    if not login or not password:
        return None
    rows = db.get_db().execute(
        "SELECT * FROM mobile_profiles WHERE basic_login = ? AND revoked = 0",
        (login,)).fetchall()
    for r in rows:
        prof = _row(r)
        if _expired(prof.get("expires_at", "")):
            continue
        if hmac.compare_digest(_kdf(password, prof.get("basic_salt", "")),
                               prof.get("basic_hash", "")):
            return prof
    return None


def touch_profile(pid: int) -> None:
    db.get_db().execute("UPDATE mobile_profiles SET last_seen = ? WHERE id = ?",
                        (db.now_iso(), int(pid)))
    db.get_db().commit()


def check_pin(prof: dict, pin: str) -> bool:
    if not prof.get("pin_hash"):
        return True
    if not pin:
        return False
    return hmac.compare_digest(_kdf(str(pin), prof.get("pin_salt", "")),
                               prof.get("pin_hash", ""))


# ─────────────────────────────────────────────────────────────── mTLS
def issue_client_cert(pid: int, days: int | None = None) -> dict:
    """Клиентский сертификат mTLS: отдаётся один раз, в БД — только отпечаток."""
    row = get(pid)
    if row.get("revoked"):
        raise MobileError(409, "профиль отозван")
    cfg = load_config()
    expires_at = row.get("expires_at") or ""
    if days is None:
        days = int(cfg.mobile.cert_days)
        if expires_at:
            left = (datetime.fromisoformat(expires_at.replace("Z", "+00:00")) - _now()).days
            days = max(1, min(days, left))
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except Exception as e:  # noqa: BLE001 — без cryptography mTLS честно недоступен
        raise MobileError(503, f"cryptography недоступен: {e!r}") from e

    key = ec.generate_private_key(ec.SECP256R1())
    cn = f"noosphere-mobile-{int(pid)}"
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Noosphere")])
    now = _now()
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=int(days)))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    fingerprint = cert.fingerprint(hashes.SHA256()).hex()
    serial = format(cert.serial_number, "x")
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    cert_expires = _iso(cert.not_valid_after_utc)
    ts = db.now_iso()
    d = db.get_db()
    d.execute("UPDATE mobile_profiles SET auth_mode = 'mtls', cert_fingerprint = ?,"
              " cert_serial = ?, cert_expires_at = ?, updated_at = ? WHERE id = ?",
              (fingerprint, serial, cert_expires, ts, int(pid)))
    d.commit()
    return {
        "profile_id": int(pid),
        "cert_pem": cert_pem,
        "key_pem": key_pem,
        "fingerprint_sha256": fingerprint,
        "serial": serial,
        "expires_at": cert_expires,
        "warning": "Сертификат и ключ показываются один раз: сервер хранит только "
                   "отпечаток. Потеряли — выпустите новый (старый перестанет проверяться).",
    }


def verify_fingerprint(fingerprint: str) -> dict | None:
    """Проверка предъявленного клиентского сертификата по отпечатку."""
    fp = (fingerprint or "").strip().replace(":", "").replace(" ", "").lower()
    if not fp:
        return None
    row = db.get_db().execute(
        "SELECT * FROM mobile_profiles WHERE cert_fingerprint = ?", (fp,)).fetchone()
    if row is None:
        return None
    prof = _row(row)
    if prof.get("revoked") or _expired(prof.get("expires_at", "")):
        return None
    return prof


# ─────────────────────────────────────────────────────────────── сессии
def _new_session(prof: dict, *, pin_ok: bool, client: str, ip: str) -> dict:
    cfg = load_config()
    ttl = int(cfg.mobile.session_ttl_hours)
    sid = uuid.uuid4().hex[:24]
    now = _now()
    expires = _iso(now + timedelta(hours=ttl)) if ttl > 0 else ""
    d = db.get_db()
    d.execute("INSERT INTO mobile_sessions(id, profile_id, created_at, last_seen,"
              " expires_at, pin_ok, closed, client, ip) VALUES(?,?,?,?,?,?,0,?,?)",
              (sid, int(prof["id"]), _iso(now), _iso(now), expires,
               1 if pin_ok else 0, (client or "")[:200], (ip or "")[:60]))
    d.commit()
    touch_profile(int(prof["id"]))
    return {"session_id": sid, "profile_id": int(prof["id"]),
            "profile": prof.get("name", ""), "pin_ok": bool(pin_ok),
            "created_at": _iso(now), "expires_at": expires, "ttl_hours": ttl}


def open_session(token: str, pin: str = "", client: str = "", ip: str = "") -> dict:
    """Открыть мобильную сессию по токену профиля (PIN проверяется сервером)."""
    prof = token_profile(token)
    if prof is None:
        raise MobileError(401, "токен отклонён")
    if prof.get("pin_hash"):
        if not (pin or "").strip():
            raise MobileError(401, "нужен PIN профиля", pin_required=True)
        if not check_pin(prof, pin):
            raise MobileError(401, "неверный PIN профиля", pin_required=True)
    sess = _new_session(prof, pin_ok=True, client=client, ip=ip)
    sess["profile"] = prof.get("name", "")
    return sess


def session(sid: str) -> dict:
    row = db.get_db().execute(
        "SELECT * FROM mobile_sessions WHERE id = ?", ((sid or "").strip(),)).fetchone()
    if row is None:
        raise MobileError(404, "сессия не найдена")
    return _row(row)


def _live(sess: dict) -> bool:
    return not sess.get("closed") and not _expired(sess.get("expires_at", ""))


def session_touch(sid: str, ip: str = "") -> None:
    d = db.get_db()
    d.execute("UPDATE mobile_sessions SET last_seen = ? WHERE id = ?",
              (db.now_iso(), (sid or "").strip()))
    if ip:
        d.execute("UPDATE mobile_sessions SET ip = ? WHERE id = ?", (ip[:60], sid.strip()))
    d.commit()


def session_lock(sid: str) -> dict:
    sess = session(sid)
    if not _live(sess):
        raise MobileError(409, "сессия закрыта")
    d = db.get_db()
    d.execute("UPDATE mobile_sessions SET pin_ok = 0 WHERE id = ?", (sess["id"],))
    d.commit()
    return {**sess, "pin_ok": 0}


def session_unlock(sid: str, pin: str) -> dict:
    sess = session(sid)
    if not _live(sess):
        raise MobileError(409, "сессия закрыта")
    prof = get(int(sess["profile_id"]))
    if prof.get("revoked"):
        raise MobileError(401, "профиль отозван")
    if not prof.get("pin_hash"):
        # у профиля PIN нет — разблокировка не требуется, сессия и так рабочая
        d = db.get_db()
        d.execute("UPDATE mobile_sessions SET pin_ok = 1 WHERE id = ?", (sess["id"],))
        d.commit()
        return {**sess, "pin_ok": 1}
    if not check_pin(prof, pin):
        raise MobileError(401, "неверный PIN профиля", pin_required=True)
    d = db.get_db()
    d.execute("UPDATE mobile_sessions SET pin_ok = 1, last_seen = ? WHERE id = ?",
              (db.now_iso(), sess["id"]))
    d.commit()
    return {**sess, "pin_ok": 1}


def session_allows_tools(sid: str, profile_id: int | None = None) -> bool:
    """Пускать ли сессию к инструментам: жива и PIN подтверждён."""
    if not (sid or "").strip():
        return False
    try:
        sess = session(sid)
    except MobileError:
        return False
    if not _live(sess) or not sess.get("pin_ok"):
        return False
    if profile_id is not None and int(sess["profile_id"]) != int(profile_id):
        return False
    return True


def close_session(sid: str) -> int:
    d = db.get_db()
    n = d.execute("UPDATE mobile_sessions SET closed = 1, closed_at = ?"
                  " WHERE id = ? AND closed = 0", (db.now_iso(), (sid or "").strip())).rowcount
    d.commit()
    return int(n or 0)


def close_all(profile_id: int | None = None) -> int:
    """Кнопка «завершить все»: гасит живые мобильные сессии (все или профиля)."""
    ts = db.now_iso()
    d = db.get_db()
    if profile_id is None:
        n = d.execute("UPDATE mobile_sessions SET closed = 1, closed_at = ?"
                      " WHERE closed = 0", (ts,)).rowcount
    else:
        n = d.execute("UPDATE mobile_sessions SET closed = 1, closed_at = ?"
                      " WHERE closed = 0 AND profile_id = ?", (ts, int(profile_id))).rowcount
    d.commit()
    return int(n or 0)


def _active_count(pid: int) -> int:
    """Живые сессии профиля: не закрыты и не истекли ('' = без срока)."""
    row = db.get_db().execute(
        "SELECT COUNT(*) FROM mobile_sessions WHERE profile_id = ? AND closed = 0"
        " AND (expires_at = '' OR expires_at > ?)",
        (int(pid), db.now_iso())).fetchone()
    return int(row[0]) if row else 0


def list_sessions(limit: int = 50, profile_id: int | None = None) -> list[dict]:
    sql = ("SELECT s.*, p.name AS profile_name FROM mobile_sessions s"
           " LEFT JOIN mobile_profiles p ON p.id = s.profile_id")
    args: list[Any] = []
    if profile_id is not None:
        sql += " WHERE s.profile_id = ?"
        args.append(int(profile_id))
    sql += " ORDER BY s.last_seen DESC LIMIT ?"
    args.append(int(limit))
    out = []
    for r in db.get_db().execute(sql, args).fetchall():
        row = _row(r)
        row["pin_ok"] = bool(row.get("pin_ok"))
        row["closed"] = bool(row.get("closed"))
        row["active"] = _live(row)
        row["expired"] = _expired(row.get("expires_at", ""))
        out.append(row)
    return out


def status(limit: int = 50) -> dict:
    """Экран состояния: профили, живые сессии, последние обращения."""
    profiles = list_profiles()
    sessions = list_sessions(limit=limit)
    active = [s for s in sessions if s["active"]]
    per_profile = []
    for p in profiles:
        mine = [s for s in sessions if int(s["profile_id"]) == int(p["id"])]
        last = max((s["last_seen"] for s in mine), default="")
        per_profile.append({
            "id": p["id"], "name": p["name"], "auth_mode": p["auth_mode"],
            "revoked": p["revoked"], "has_pin": p["has_pin"],
            "active_sessions": len([s for s in mine if s["active"]]),
            "last_seen": max(last, p.get("last_seen", "")),
            "last_request": last,
        })
    return {
        "ts": db.now_iso(),
        "profiles": profiles,
        "per_profile": per_profile,
        "sessions": sessions,
        "active_sessions": len(active),
        "total_sessions": len(sessions),
    }


# ─────────────────────────────────────────────────────────────── SSH-туннель и QR
def _split_addr(addr: str) -> tuple[str, str, int]:
    """user@host[:port] → (user, host, ssh_port). Пустой user = взять владельца."""
    addr = _norm_addr(addr)
    user, host = "", addr
    if "@" in addr:
        user, host = addr.rsplit("@", 1)
    port = 22
    if host.count(":") == 1 and host.rsplit(":", 1)[1].isdigit():
        host, raw_port = host.rsplit(":", 1)
        port = int(raw_port)
    return user.strip(), host.strip(), port


def tunnel(prof: dict | None = None, pid: int | None = None) -> dict:
    """Команда туннеля по SSH + адрес для телефона. Порт — из конфига сервиса.

    Форма контракта: `ssh -N -L <port>:127.0.0.1:<port> user@host`.
    """
    if prof is None:
        if pid is None:
            raise MobileError(400, "не задан профиль")
        prof = get(int(pid))
    cfg = load_config()
    port = int(cfg.server.port)                 # ← единственный источник порта
    host_cfg = getattr(cfg.server, "host", "") or ""
    user, host, ssh_port = _split_addr(prof.get("server_addr", ""))
    if not host or host in ("0.0.0.0", "::", "127.0.0.1", "localhost"):
        # адрес сервера в профиле пустой/локальный — берём внешний вид хоста из конфига
        host = host_cfg if host_cfg not in ("", "0.0.0.0", "::", "127.0.0.1") else \
            cfg.mobile.loopback_host
    user = user or cfg.owner_user
    ssh_opt = f" -p {ssh_port}" if ssh_port != 22 else ""
    command = f"ssh -N -L {port}:127.0.0.1:{port}{ssh_opt} {user}@{host}"
    # в одну строку без -p, когда порт ssh стандартный — так подсказка совпадает
    # с той формой, которую владелец копирует руками
    loopback = f"http://{cfg.mobile.loopback_host}:{port}/"
    return {
        "profile_id": prof.get("id"),
        "port": port,
        "port_source": "server.port",
        "ssh_user": user,
        "ssh_host": host,
        "ssh_port": ssh_port,
        "command": command,
        "loopback_url": loopback,
        "forward": f"{port}:127.0.0.1:{port}",
        "hint": "Скопировать в терминал Мака — телефон получит доступ к "
                f"{loopback} через 127.0.0.1.",
    }


def tunnel_qr(prof: dict | None = None, pid: int | None = None) -> dict:
    """Данные и готовые SVG двух QR: команда туннеля и loopback-ссылка."""
    from . import qr

    t = tunnel(prof, pid)
    return {
        **t,
        "qr_command": qr.svg(t["command"], scale=5, border=4,
                             label="SSH-туннель: " + t["command"]),
        "qr_url": qr.svg(t["loopback_url"], scale=5, border=4,
                         label="Мобильный адрес " + t["loopback_url"]),
        "qr_payloads": {"command": t["command"], "url": t["loopback_url"]},
    }


def dump_session(sess: dict) -> str:
    """Отладочный дамп сессии без секретов (только id/времена/флаги)."""
    safe = {k: v for k, v in sess.items()
            if k not in ("token", "pin", "pin_hash", "pin_salt", "token_hash")}
    return json.dumps(safe, ensure_ascii=False, sort_keys=True)
