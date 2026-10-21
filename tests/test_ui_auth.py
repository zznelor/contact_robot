"""Панель: первый запуск, вход, приглашения, роли, команда."""

import asyncio
import html
import json
import re
import threading

import httpx

import pytest
from fastapi.testclient import TestClient

from conftest import ORIGIN, login_client, run
from first_contact import auth
from first_contact.auth import SESSION_COOKIE


def is_error(r) -> bool:
    """Переход с сообщением об ошибке: «!» в начале msg (ui_base.go)."""
    return "msg=%21" in r.headers["location"]


# ---------- первый запуск ----------

def test_without_admin_goes_to_setup(panel):
    r = TestClient(panel.app).get("/", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/setup")


@pytest.mark.parametrize("code, email, password, headers", [
    ("нет", "a@x.ru", "12345678", ORIGIN),        # неверный код
    (None, "a@x.ru", "1234", ORIGIN),             # короткий пароль
    (None, "x'@a.ru", "12345678", ORIGIN),        # почта с кавычкой
    (None, "A@x.ru", "12345678", {}),             # форма не со страницы панели
])
def test_setup_rejected(panel, code, email, password, headers):
    real = panel.users.setup_code()
    r = TestClient(panel.app).post("/setup", data={"code": code or real, "email": email, "password": password},
                                   headers=headers, follow_redirects=False)
    assert is_error(r)
    assert panel.users.count() == 0


def test_setup_creates_admin_and_closes(panel):
    c = TestClient(panel.app)
    code = panel.users.setup_code()
    r = c.post("/setup", data={"code": code, "email": "A@x.ru", "password": "12345678", "name": "Илья"},
               headers=ORIGIN, follow_redirects=False)
    assert r.headers["location"].startswith("/settings")
    assert SESSION_COOKIE in r.cookies
    assert panel.users.count() == 1 and panel.users.by_email("a@x.ru")["role"] == "admin"
    assert panel.users.setup_code() is None
    assert c.get("/setup", follow_redirects=False).headers["location"] == "/login"


# ---------- вход и выход ----------

def test_login_with_right_password(panel):
    panel.users.create("a@x.ru", "12345678", "admin")
    r = TestClient(panel.app).post("/login", data={"email": "a@x.ru", "password": "12345678"}, headers=ORIGIN,
                                   follow_redirects=False)
    assert r.headers["location"] == "/" and SESSION_COOKIE in r.cookies


def test_login_wrong_password(panel):
    panel.users.create("a@x.ru", "12345678", "admin")
    r = TestClient(panel.app).post("/login", data={"email": "a@x.ru", "password": "wrong-pass"}, headers=ORIGIN,
                                   follow_redirects=False)
    assert is_error(r) and SESSION_COOKIE not in r.cookies


def test_login_blocked_after_10_failures(panel):
    panel.users.create("a@x.ru", "12345678", "admin")
    c = TestClient(panel.app)
    for _ in range(10):
        c.post("/login", data={"email": "a@x.ru", "password": "wrong-pass"}, headers=ORIGIN)
    r = c.post("/login", data={"email": "a@x.ru", "password": "12345678"}, headers=ORIGIN, follow_redirects=False)
    assert r.headers["location"].startswith("/login") and SESSION_COOKIE not in r.cookies


def test_logout_requires_login_again(panel):
    op = login_client(panel, "op@x.ru", "operator")
    assert op.get("/").status_code == 200
    op.post("/logout")
    assert op.get("/", follow_redirects=False).status_code == 303


# ---------- приглашения и роли ----------

def invite_token(admin) -> str:
    r = admin.post("/team/invite", data={"role": "operator"})
    return re.search(r"/invite/([\w-]+)", r.text).group(1)


def test_invite_link_shown_in_team_section(admin):
    r = admin.post("/team/invite", data={"role": "operator"})
    assert r.status_code == 200
    assert len(re.search(r"/invite/([\w-]+)", r.text).group(1)) > 20
    assert 'id="team"' in r.text


def test_invite_rejects_email_with_quote_without_spending(panel, admin):
    token = invite_token(admin)
    evil = "x'-alert(1)-'@a.ru"
    r = TestClient(panel.app).post(f"/invite/{token}", data={"email": evil, "password": "operator1"},
                                   headers=ORIGIN, follow_redirects=False)
    assert is_error(r) and panel.users.by_email(evil) is None
    assert panel.users.invite(token) is not None, "неудачная попытка не тратит приглашение"


def test_invite_registers_operator_once(panel, admin):
    token = invite_token(admin)
    c = TestClient(panel.app)
    c.post(f"/invite/{token}", data={"email": "op@x.ru", "password": "operator1"}, headers=ORIGIN)
    assert panel.users.by_email("op@x.ru")["role"] == "operator"
    c.post(f"/invite/{token}", data={"email": "op2@x.ru", "password": "operator1"}, headers=ORIGIN)
    assert panel.users.by_email("op2@x.ru") is None, "приглашение одноразовое"


def test_operator_sees_deals_but_not_settings(configured):
    run(configured.worker.enable())
    op = login_client(configured, "op@x.ru", "operator")
    assert op.get("/").status_code == 200
    assert op.get("/settings").status_code == 403
    assert op.post("/settings/disable").status_code == 403 and configured.s.enabled
    assert 'href="/settings"' not in op.get("/").text, "у оператора нет пункта «Настройки»"


def delete_confirm_text(page: str, email: str) -> str:
    """Строка, которую покажет confirm() у кнопки удаления: атрибут → JS-литерал → текст."""
    for attr in re.findall(r'onsubmit="return confirm\((.*?)\)"', page):
        text = json.loads(html.unescape(attr))
        if email in text:
            return text
    raise AssertionError(f"нет кнопки удаления для {email}")


@pytest.mark.parametrize("email", ["op@x.ru", "o'neil@x.ru"])   # кавычка — адрес, заведённый до проверки почты
def test_delete_confirm_is_valid_js_string(panel, admin, email):
    panel.users.create(email, "password1", "operator")
    assert email in delete_confirm_text(admin.get("/settings").text, email)


def test_cannot_delete_self(panel, admin):
    me = panel.users.by_email("a@x.ru")["id"]
    r = admin.post(f"/team/{me}/delete", follow_redirects=False)
    assert r.status_code == 303 and panel.users.by_id(me) is not None


def test_team_is_settings_section_not_top_menu(admin):
    assert 'href="/team"' not in admin.get("/").text
    assert 'href="#team"' in admin.get("/settings").text
    r = admin.get("/team", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/settings#team")


def test_login_password_check_does_not_block_event_loop(panel, monkeypatch):
    """Пока считается scrypt входа, цикл событий обслуживает остальных (вебхуки, проход робота)."""
    panel.users.create("a@x.ru", "password1", "admin")
    started, released, outcome = threading.Event(), threading.Event(), []
    real = auth.hashlib.scrypt

    def scrypt(*a, **kw):
        started.set()
        outcome.append(released.wait(5))   # дождался ли другой корутины, пока шёл расчёт
        return real(*a, **kw)
    monkeypatch.setattr(auth.hashlib, "scrypt", scrypt)

    async def scenario():
        transport = httpx.ASGITransport(app=panel.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", headers=ORIGIN) as c:
            login = asyncio.create_task(c.post("/login", data={"email": "a@x.ru", "password": "password1"}))
            await asyncio.to_thread(started.wait, 5)
            released.set()                 # выполнится, только если цикл событий не занят расчётом
            return await login
    r = run(scenario())
    assert outcome == [True]
    assert r.status_code == 303 and r.headers["location"] == "/"
