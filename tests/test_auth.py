"""Пароли, проверка почты и защита от перебора (auth.py)."""

import os
import time as time_mod
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from first_contact import auth
from first_contact.auth import RateLimit, Users, check_password, email_problem, hash_password


def test_password_roundtrip():
    stored = hash_password("correct horse")
    assert check_password("correct horse", stored)
    assert not check_password("wrong horse", stored)


def test_password_hash_salted_and_not_plain():
    a, b = hash_password("same-pass"), hash_password("same-pass")
    assert a != b
    assert "same-pass" not in a


@pytest.mark.parametrize("stored", ["", "garbage", "scrypt$only-two"])
def test_check_password_malformed_hash_is_false(stored):
    assert check_password("anything", stored) is False


@pytest.mark.parametrize("email", ["name@company.ru", "  a@x.ru  ", "first.last+tag@mail.co.uk", "A@X.RU",
                                   # 254 символа — предел длины адреса (RFC 5321, 4.5.3.1.3), метки домена до 63
                                   "a@" + ".".join(["b" * 63, "c" * 63, "d" * 63, "e" * 60])])
def test_email_ok(email):
    assert email_problem(email) is None


@pytest.mark.parametrize("email", [
    "x'-alert(1)-'@a.ru",       # кавычки и скобки — почта выводится в панели, в том числе в подсказках
    '"a"@x.ru',
    "<a@x.ru>",
    "a b@x.ru",                 # пробел
    "a@x",                      # без домена верхнего уровня
    "a@@x.ru",
    "",
    "a" * 250 + "@x.ru",        # длиннее 254
])
def test_email_problem(email):
    assert email_problem(email) is not None


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(auth, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def test_rate_limit_blocks_after_limit_per_address(clock):
    rl = RateLimit(limit=3, window=900)
    for _ in range(2):
        rl.fail("1.1.1.1")
    assert not rl.blocked("1.1.1.1")
    rl.fail("1.1.1.1")
    assert rl.blocked("1.1.1.1")
    assert not rl.blocked("2.2.2.2"), "другой адрес не заблокирован"


def test_panel_rate_limit_is_10_failures_per_15_minutes(clock):
    """docs/УСТАНОВКА.md: после 10 неверных паролей вход с этого адреса блокируется на 15 минут."""
    rl = RateLimit()
    for _ in range(9):
        rl.fail("1.1.1.1")
    assert not rl.blocked("1.1.1.1")
    rl.fail("1.1.1.1")
    clock[0] += 15 * 60 - 1
    assert rl.blocked("1.1.1.1")
    clock[0] += 1
    assert not rl.blocked("1.1.1.1")


def test_rate_limit_window_expires(clock):
    rl = RateLimit(limit=3, window=900)
    for _ in range(3):
        rl.fail("1.1.1.1")
    clock[0] += 899
    assert rl.blocked("1.1.1.1")
    clock[0] += 1
    assert not rl.blocked("1.1.1.1")


# ---------- сроки приглашения и сессии ----------

@contextmanager
def process_tz(name: str):
    """Часовой пояс процесса (TZ) на время блока: сроки не должны зависеть от пояса машины, где запущен робот."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = name
    time_mod.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time_mod.tzset()


@pytest.fixture
def later(monkeypatch):
    """Перевести «сейчас» проверок срока (auth.now) вперёд на timedelta."""
    def shift(delta: timedelta) -> None:
        monkeypatch.setattr(auth, "now", lambda: (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds"))
    return shift


@pytest.mark.parametrize("tz", ["Etc/GMT+12", "Etc/GMT-14"])     # UTC−12 и UTC+14: самые дальние от UTC
@pytest.mark.parametrize("what, days", [("invite", 7), ("session", 30)])   # docs/НАСТРОЙКА.md, docs/УСТАНОВКА.md
def test_invite_and_session_last_exactly_their_days(db, later, tz, what, days):
    users = Users(db)
    uid = users.create("a@x.ru", "password1", "admin")
    with process_tz(tz):
        if what == "invite":
            token = users.new_invite("operator", uid)
            valid = lambda: users.invite(token) is not None          # noqa: E731
        else:
            token = users.new_session(uid)
            valid = lambda: users.by_session(token) is not None      # noqa: E731
    later(timedelta(days=days, minutes=-1))
    assert valid()
    later(timedelta(days=days, minutes=1))
    assert not valid()
