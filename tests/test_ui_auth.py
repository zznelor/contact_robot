"""Панель: первый запуск, вход, приглашения, роли, команда."""

import asyncio
import html
import json
import re
import threading
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import httpx

import pytest
from fastapi.testclient import TestClient

from conftest import ORIGIN, login_client, run
from first_contact import auth
from first_contact.auth import SESSION_COOKIE


def is_error(r) -> bool:
    """Переход с сообщением об ошибке: «!» в начале msg в строке запроса адреса (ui_base.go). Именно в запросе:
    после «#» браузер его серверу не отправит, и сообщения на странице не будет."""
    return parse_qs(urlsplit(r.headers["location"]).query).get("msg", [""])[0].startswith("!")


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
    admin = panel.users.by_email("a@x.ru")
    assert panel.users.count() == 1 and (admin["role"], admin["name"]) == ("admin", "Илья")
    assert panel.users.setup_code() is None
    assert c.get("/setup", follow_redirects=False).headers["location"] == "/login"


# ---------- вход и выход ----------

def test_login_with_right_password(panel):
    panel.users.create("a@x.ru", "12345678", "admin")
    r = TestClient(panel.app).post("/login", data={"email": "a@x.ru", "password": "12345678"}, headers=ORIGIN,
                                   follow_redirects=False)
    assert r.headers["location"] == "/" and SESSION_COOKIE in r.cookies


@pytest.mark.parametrize("email, password", [
    ("a@x.ru", "wrong-pass"),         # неверный пароль
    ("nobody@x.ru", "12345678"),      # такого пользователя нет
])
def test_login_wrong_password(panel, email, password):
    panel.users.create("a@x.ru", "12345678", "admin")
    r = TestClient(panel.app).post("/login", data={"email": email, "password": password}, headers=ORIGIN,
                                   follow_redirects=False)
    assert r.status_code == 303 and is_error(r) and SESSION_COOKIE not in r.cookies


def test_login_blocked_after_10_failures(panel):
    """docs/УСТАНОВКА.md: после 10 неверных паролей вход с этого адреса блокируется — с другого адреса входят."""
    panel.users.create("a@x.ru", "12345678", "admin")
    c = TestClient(panel.app, client=("10.0.0.1", 50000))
    for _ in range(10):
        c.post("/login", data={"email": "a@x.ru", "password": "wrong-pass"}, headers=ORIGIN)
    r = c.post("/login", data={"email": "a@x.ru", "password": "12345678"}, headers=ORIGIN, follow_redirects=False)
    assert r.headers["location"].startswith("/login") and SESSION_COOKIE not in r.cookies
    other = TestClient(panel.app, client=("10.0.0.2", 50000))
    r = other.post("/login", data={"email": "a@x.ru", "password": "12345678"}, headers=ORIGIN, follow_redirects=False)
    assert r.headers["location"] == "/" and SESSION_COOKIE in r.cookies


@pytest.mark.parametrize("base_url, headers, secure", [
    ("http://testserver", {}, False),                              # свой компьютер, без HTTPS
    ("http://testserver", {"X-Forwarded-Proto": "https"}, True),   # за Caddy: снаружи HTTPS
    ("https://testserver", {}, True),
])
def test_session_cookie_attributes(panel, base_url, headers, secure):
    """Cookie сессии: HttpOnly, SameSite=Lax (auth.py), 30 дней (docs/УСТАНОВКА.md), Secure — когда панель открыта
    по HTTPS; по ней же пускает в панель."""
    panel.users.create("a@x.ru", "12345678", "admin")
    r = TestClient(panel.app, base_url=base_url).post(
        "/login", data={"email": "a@x.ru", "password": "12345678"}, headers={**ORIGIN, **headers},
        follow_redirects=False)
    cookie = SimpleCookie(r.headers["set-cookie"])[SESSION_COOKIE]
    assert (cookie["httponly"], cookie["samesite"].lower(), cookie["max-age"]) == (True, "lax", str(30 * 86400))
    assert bool(cookie["secure"]) is secure
    c = TestClient(panel.app, cookies={SESSION_COOKIE: cookie.value})
    assert c.get("/", follow_redirects=False).status_code == 200


@pytest.mark.parametrize("headers, allowed", [
    ({}, False),                                                 # ни Origin, ни Referer
    ({"Origin": "http://evil.example"}, False),
    ({"Origin": "null"}, False),                                 # sandbox-iframe, file://
    ({"Referer": "http://evil.example/page"}, False),
    ({"Referer": "http://testserver/settings"}, True),           # Origin не прислан — сверяется Referer
])
def test_form_from_other_site_refused(configured, headers, allowed):
    """Формы панели принимаются только со своих страниц (проверка Origin/Referer, auth.py): чужой сайт не
    выключит робота cookie вошедшего администратора."""
    run(configured.worker.enable())
    uid = configured.users.create("a@x.ru", "password1", "admin")
    c = TestClient(configured.app, cookies={SESSION_COOKIE: configured.users.new_session(uid)})
    r = c.post("/settings/disable", headers=headers, follow_redirects=False)
    assert (r.status_code, configured.s.enabled) == ((303, False) if allowed else (403, True))


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


@pytest.mark.parametrize("headers, base", [
    ({}, "http://testserver"),
    # За Caddy (docs/УСТАНОВКА.md): адрес панели — из X-Forwarded-Proto и X-Forwarded-Host, форма — с того же адреса
    ({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "robot.company.ru", "Origin": "https://robot.company.ru"},
     "https://robot.company.ru"),
])
def test_invite_link_is_full_panel_address(panel, admin, headers, base):
    """Ссылку-приглашение администратор отправляет человеку — это полный адрес панели, по которому она открывается."""
    r = admin.post("/team/invite", data={"role": "operator"}, headers=headers)
    token = re.search(r"/invite/([\w-]+)", r.text).group(1)
    assert f"{base}/invite/{token}" in r.text
    assert panel.users.invite(token) is not None


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
    c.post(f"/invite/{token}", data={"email": "op@x.ru", "password": "operator1", "name": "Ольга"}, headers=ORIGIN)
    op = panel.users.by_email("op@x.ru")
    assert (op["role"], op["name"]) == ("operator", "Ольга")
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


def post_at_once(panel, monkeypatch, path: str | list[str], forms: list[dict]):
    """Формы отправлены одновременно: каждый запрос доходит до расчёта scrypt, пока другой не дошёл до него же.
    path — один адрес для всех форм или свой адрес каждой."""
    paths = [path] * len(forms) if isinstance(path, str) else path
    barrier = threading.Barrier(len(forms), timeout=5)
    real = auth.hashlib.scrypt

    def scrypt(*a, **kw):
        barrier.wait()
        return real(*a, **kw)
    monkeypatch.setattr(auth.hashlib, "scrypt", scrypt)

    async def scenario():
        transport = httpx.ASGITransport(app=panel.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", headers=ORIGIN) as c:
            return await asyncio.gather(*(c.post(p, data=f) for p, f in zip(paths, forms)))
    return run(scenario())


def test_invite_used_once_by_simultaneous_requests(panel, admin, monkeypatch):
    token = invite_token(admin)
    emails = ["op1@x.ru", "op2@x.ru"]
    post_at_once(panel, monkeypatch, f"/invite/{token}", [{"email": m, "password": "operator1"} for m in emails])
    assert len([m for m in emails if panel.users.by_email(m)]) == 1, "приглашение одноразовое"
    assert panel.users.count() == 2


def test_two_invites_for_one_email_at_once_create_one_user(panel, admin, monkeypatch):
    """Два приглашения, одна почта, одновременно: учётная запись одна, второму — отказ с сообщением (не 500),
    его приглашение не потрачено."""
    tokens = [invite_token(admin), invite_token(admin)]
    rs = post_at_once(panel, monkeypatch, [f"/invite/{t}" for t in tokens],
                      [{"email": "op@x.ru", "password": "operator1"}] * 2)
    assert panel.users.count() == 2
    assert sorted((r.status_code, is_error(r)) for r in rs) == [(303, False), (303, True)]
    assert len([t for t in tokens if panel.users.invite(t) is not None]) == 1


def test_setup_creates_one_admin_from_simultaneous_requests(panel, monkeypatch):
    code = panel.users.setup_code()
    post_at_once(panel, monkeypatch, "/setup", [{"code": code, "email": m, "password": "12345678"}
                                                for m in ("a1@x.ru", "a2@x.ru")])
    assert panel.users.count() == 1


def sessions_of(panel, uid: int) -> int:
    return panel.db.conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (uid,)).fetchone()[0]


def test_login_with_old_password_during_password_change_gets_no_session(panel, monkeypatch):
    """Вход со старым паролем: пока проверяется пароль, администратор сервера задал новый (reset-password закрывает
    все сессии). Проверка старого пароля, закончившаяся после смены, сессию не выдаёт."""
    uid = panel.users.create("a@x.ru", "password1", "admin")
    checked, release = threading.Event(), threading.Event()
    real = auth.check_password

    def check(password, stored):
        ok = real(password, stored)
        checked.set()
        release.wait(5)
        return ok
    monkeypatch.setattr(auth, "check_password", check)

    async def scenario():
        transport = httpx.ASGITransport(app=panel.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", headers=ORIGIN) as c:
            login = asyncio.create_task(c.post("/login", data={"email": "a@x.ru", "password": "password1"}))
            await asyncio.to_thread(checked.wait, 5)
            panel.users.set_password(uid, "password2")
            release.set()
            return await login
    r = run(scenario())
    assert is_error(r)
    assert sessions_of(panel, uid) == 0


def test_session_not_created_for_password_changed_after_check(panel):
    """Пароль проверен, а до создания сессии его сменили из другого процесса (reset-password): сессия по
    проверенному, уже не действующему паролю не создаётся."""
    uid = panel.users.create("a@x.ru", "password1", "admin")
    user = run(panel.users.login_async("a@x.ru", "password1"))
    panel.users.set_password(uid, "password2")
    assert panel.users.new_session(uid, pw_hash=user["pw_hash"]) is None
    assert sessions_of(panel, uid) == 0
