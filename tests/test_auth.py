"""Пароли, проверка почты и защита от перебора (auth.py)."""

from types import SimpleNamespace

import pytest

from first_contact import auth
from first_contact.auth import RateLimit, check_password, email_problem, hash_password


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


@pytest.mark.parametrize("email", ["name@company.ru", "  a@x.ru  ", "first.last+tag@mail.co.uk", "A@X.RU"])
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


def test_rate_limit_window_expires(clock):
    rl = RateLimit(limit=3, window=900)
    for _ in range(3):
        rl.fail("1.1.1.1")
    clock[0] += 899
    assert rl.blocked("1.1.1.1")
    clock[0] += 1
    assert not rl.blocked("1.1.1.1")
