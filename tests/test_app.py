"""HTTP-входы app.py: вебхуки Орбиты и Битрикса, /healthz и /ready. Логика событий — test_worker.py."""

import pytest
from fastapi.testclient import TestClient

from conftest import post_orbita, wait_for
from first_contact.app import create_app

RECEIVED = {"type": "message.received", "data": {"dialog_id": 501, "direction": "incoming"}}


@pytest.fixture
def client(webhook_app):
    # В контексте: один цикл событий на все запросы, фоновые задачи вебхука в нём доживают до конца
    with TestClient(webhook_app) as c:
        yield c


@pytest.fixture
def waiting_deal(worker):
    worker.db.add_deal(10)
    worker.db.update(10, status="sent_first", dialog_id="501", phone="+7")
    return worker


# ---------- вебхук Орбиты ----------

@pytest.mark.parametrize("secret", ["bad", None])
def test_orbita_webhook_bad_signature_401(webhook_app, waiting_deal, secret):
    c = TestClient(webhook_app)
    if secret is None:
        r = c.post("/orbita/webhook", json={"id": 1, **RECEIVED})
    else:
        r = post_orbita(c, {"id": 1, **RECEIVED}, secret=secret, event_id="1")
    assert r.status_code == 401
    assert waiting_deal.db.deal(10)["status"] == "sent_first"


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
def test_orbita_webhook_malformed_body_400(webhook_app, body):
    assert post_orbita(TestClient(webhook_app), body, event_id="1").status_code == 400


def test_orbita_webhook_accepted_and_processed(client, waiting_deal):
    r = post_orbita(client, {"id": 2, **RECEIVED}, event_id="2")
    assert r.status_code == 200 and r.json()["ok"]
    wait_for(lambda: waiting_deal.db.deal(10)["status"] == "replied")


def test_orbita_webhook_duplicate_event_cut_off(client, waiting_deal):
    c = client
    assert not post_orbita(c, {"id": 2, **RECEIVED}, event_id="2").json().get("duplicate")
    assert post_orbita(c, {"id": 2, "type": "message.received", "data": {}}, event_id="2").json().get("duplicate")


def test_orbita_events_without_id_are_not_duplicates(client, waiting_deal):
    r1 = post_orbita(client, {"type": "channel.connected", "data": {"provider": "max"}})
    r2 = post_orbita(client, RECEIVED)
    assert not r1.json().get("duplicate") and not r2.json().get("duplicate")
    wait_for(lambda: waiting_deal.db.deal(10)["status"] == "replied")


def test_orbita_processing_failure_logged_and_redelivery_processed(client, waiting_deal, monkeypatch):
    original = waiting_deal.on_orbita_event

    async def broken(ev):
        raise RuntimeError("сбой")
    monkeypatch.setattr(waiting_deal, "on_orbita_event", broken)
    post_orbita(client, {"id": 50, **RECEIVED}, event_id="50")
    # Название записи журнала задано в docs/РАЗРАБОТЧИКУ.md, «Защита от дублей»
    wait_for(lambda: any(ev["event"] == "Орбита: ошибка обработки события" for ev in waiting_deal.db.events()))

    monkeypatch.setattr(waiting_deal, "on_orbita_event", original)
    r = post_orbita(client, {"id": 50, **RECEIVED}, event_id="50")
    assert not r.json().get("duplicate"), "номер события забыт — повторная доставка проходит"
    wait_for(lambda: waiting_deal.db.deal(10)["status"] == "replied")


# ---------- событие Битрикса ----------

@pytest.mark.parametrize("form, status", [
    ({"auth[application_token]": "T", "event": "ONCRMDEALADD", "data[FIELDS][ID]": "abc"}, 400),
    ({"auth[application_token]": "wrong", "event": "ONCRMDEALADD", "data[FIELDS][ID]": "5"}, 403),
    ({"event": "ONCRMDEALADD", "data[FIELDS][ID]": "5"}, 403),
])
def test_bitrix_deal_add_rejects(webhook_app, worker, form, status):
    worker.s.bitrix_app_token = "T"
    assert TestClient(webhook_app).post("/bitrix/deal-add", data=form).status_code == status
    assert worker.db.deal(5) is None


# ---------- проверки для Kubernetes ----------

def test_healthz_always_200(worker):
    c = TestClient(create_app(worker))
    assert c.get("/healthz").status_code == 200
    worker.db.conn.close()
    assert c.get("/healthz").status_code == 200, "liveness не зависит от базы"


def test_ready_200_with_working_db(worker):
    assert TestClient(create_app(worker)).get("/ready").status_code == 200


def test_ready_503_when_db_unavailable(worker):
    c = TestClient(create_app(worker))
    worker.db.conn.close()
    assert c.get("/ready").status_code == 503
