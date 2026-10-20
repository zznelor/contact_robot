"""Пользователи панели: вход по почте и паролю, роли, приглашения.

- Пароли — scrypt из стандартной библиотеки (без внешних зависимостей).
- Сессия — случайный токен в cookie; в базе лежит только его хэш.
- Формы защищены проверкой Origin/Referer (запрос должен прийти со своей же страницы)
  и cookie SameSite=Lax.
- Первый администратор создаётся на странице /setup по одноразовому коду из лога сервиса
  (или командой `python -m first_contact create-admin`).

Роли: admin — всё, включая настройки и команду; operator — сделки и журнал.
"""

import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .db import DB, now

SESSION_COOKIE = "fc_session"
SESSION_DAYS = 30
INVITE_DAYS = 7
ROLES = {"admin": "администратор", "operator": "оператор"}
MIN_PASSWORD = 8


# ---------- пароли ----------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(got.hex(), digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Почта без кавычек, пробелов, скобок и угловых скобок: она выводится в панели, в том числе в подсказках
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")


def email_problem(email: str) -> str | None:
    email = email.strip()
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        return "почта выглядит неправильно — нужен адрес вида name@company.ru"
    return None


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"пароль короче {MIN_PASSWORD} символов"
    return None


# ---------- пользователи ----------

class Users:
    def __init__(self, db: DB):
        self.db = db
        self.c = db.conn

    def count(self) -> int:
        return self.c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def all(self):
        return self.c.execute("SELECT * FROM users ORDER BY id").fetchall()

    def by_email(self, email: str):
        return self.c.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()

    def by_id(self, user_id: int):
        return self.c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def create(self, email: str, password: str, role: str, name: str = "") -> int:
        assert role in ROLES
        cur = self.c.execute(
            "INSERT INTO users (email, name, role, pw_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (email.strip().lower(), name.strip(), role, hash_password(password), now()))
        return cur.lastrowid

    def set_password(self, user_id: int, password: str) -> None:
        self.c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(password), user_id))
        self.c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def delete(self, user_id: int) -> None:
        self.c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        self.c.execute("DELETE FROM users WHERE id=?", (user_id,))

    def admins(self) -> int:
        return self.c.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]

    # ---------- сессии ----------

    def login(self, email: str, password: str):
        u = self.by_email(email)
        if u is None:
            hash_password(password)          # одинаковое время ответа, есть такой пользователь или нет
            return None
        return u if check_password(password, u["pw_hash"]) else None

    def new_session(self, user_id: int) -> str:
        self.c.execute("DELETE FROM sessions WHERE expires_at<=?", (now(),))   # заодно чистим истёкшие
        token = secrets.token_urlsafe(32)
        exp = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
        self.c.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                       (_token_hash(token), user_id, exp.isoformat(timespec="seconds")))
        return token

    def by_session(self, token: str | None):
        if not token:
            return None
        row = self.c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
            (_token_hash(token), now())).fetchone()
        return row

    def end_session(self, token: str | None) -> None:
        if token:
            self.c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))

    # ---------- приглашения ----------

    def new_invite(self, role: str, created_by: int) -> str:
        assert role in ROLES
        self.c.execute("DELETE FROM invites WHERE expires_at<=?", (now(),))
        token = secrets.token_urlsafe(24)
        exp = datetime.now(timezone.utc) + timedelta(days=INVITE_DAYS)
        self.c.execute("INSERT INTO invites (token_hash, role, created_by, expires_at) VALUES (?, ?, ?, ?)",
                       (_token_hash(token), role, created_by, exp.isoformat(timespec="seconds")))
        return token

    def invite(self, token: str):
        return self.c.execute(
            "SELECT * FROM invites WHERE token_hash=? AND used_at IS NULL AND expires_at>?",
            (_token_hash(token), now())).fetchone()

    def use_invite(self, token: str) -> None:
        self.c.execute("UPDATE invites SET used_at=? WHERE token_hash=?", (now(), _token_hash(token)))

    def open_invites(self):
        return self.c.execute("SELECT * FROM invites WHERE used_at IS NULL AND expires_at>? ORDER BY expires_at",
                              (now(),)).fetchall()

    # ---------- первый запуск ----------

    def setup_code(self) -> str | None:
        """Одноразовый код для создания первого администратора. None — администратор уже есть."""
        if self.count():
            self.db.put("setup_code", None)
            return None
        code = self.db.get("setup_code")
        if not code:
            code = "-".join(secrets.token_hex(2) for _ in range(3))
            self.db.put("setup_code", code)
        return code


# ---------- защита от перебора и подделки форм ----------

class RateLimit:
    """Не больше `limit` неудачных попыток входа с одного адреса за `window` секунд."""

    def __init__(self, limit: int = 10, window: int = 900):
        self.limit, self.window = limit, window
        self.fails: dict[str, list[float]] = defaultdict(list)

    def blocked(self, key: str) -> bool:
        t = time.monotonic()
        for k in list(self.fails):                      # старые адреса не копятся в памяти
            self.fails[k] = [x for x in self.fails[k] if t - x < self.window]
            if not self.fails[k]:
                del self.fails[k]
        return len(self.fails.get(key, ())) >= self.limit

    def fail(self, key: str) -> None:
        self.fails[key].append(time.monotonic())


def same_origin(request) -> bool:
    """Форма отправлена со страницы этого же сайта."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return False
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urlsplit(source).netloc == host
