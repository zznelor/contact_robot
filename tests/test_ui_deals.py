"""Панель: список и карточка сделки, ручные действия, шаги робота по кнопке, поиск."""

import pytest

from conftest import run

from test_ui_auth import is_error


@pytest.fixture
def deals(configured):
    run(configured.worker.enable())
    db = configured.db
    db.add_deal(41)
    db.update(41, status="needs_check", name="Сергей", phone="+7999", last_error="x")
    db.add_deal(42)
    db.update(42, status="replied", name="Игорь", reply_due_at="2030-01-01T00:00:00+00:00")
    return configured


@pytest.mark.parametrize("path", ["/", "/?status=needs_check", "/?q=игорь", "/deal/41", "/deal/42", "/deal/999",
                                  "/events", "/status", "/account", "/settings", "/team"])
def test_pages_open(deals, admin, path):
    assert admin.get(path).status_code == 200


def test_stop_on_replied_cancels_template_reply(deals, admin):
    admin.post("/deal/42/stop")
    row = deals.db.deal(42)
    assert row["status"] == "replied" and not row["reply_due_at"]


def test_manual_client_replied(deals, admin):
    admin.post("/deal/41/replied")
    row = deals.db.deal(41)
    assert row["status"] == "replied" and row["client_replied_at"]


@pytest.mark.parametrize("action", ["replied", "stop"])
def test_manual_action_on_missing_deal_refused(deals, admin, action):
    r = admin.post(f"/deal/99999/{action}", follow_redirects=False)
    assert is_error(r)
    assert deals.db.deal(99999) is None and deals.db.events(99999) == []


def test_steps_offered_on_main_page(deals, admin):
    page = admin.get("/").text
    assert 'name="step"' in page
    assert all(f'value="{step}"' in page for step in deals.worker.STEPS)


def test_one_step_by_button(deals, admin):
    r = admin.post("/actions/tick", data={"step": "pending"}, follow_redirects=False)
    assert r.headers["location"].startswith("/?msg=") and not is_error(r)
    assert deals.s.enabled


def test_unknown_step_by_button_refused(deals, admin):
    assert is_error(admin.post("/actions/tick", data={"step": "oops"}, follow_redirects=False))


def test_search_by_name_case_insensitive(deals, admin):
    page = admin.get("/?q=игорь").text
    assert 'href="/deal/42"' in page and 'href="/deal/41"' not in page


def test_search_and_counter_over_whole_base(deals, admin):
    db = deals.db
    db.conn.execute("BEGIN")
    for i in range(1100, 2300):
        db.add_deal(i)
    db.conn.execute("COMMIT")
    assert 'href="/deal/42"' in admin.get("/?q=игорь").text, "находит и за пределами последних 1000"
    rows, total = db.find()
    assert (len(rows), total) == (300, 1202)
    assert "1202" in admin.get("/").text, "«показано из» — по всей базе"
