"""HTTP-входы app.py: вебхуки Орбиты и Битрикса, /healthz и /ready. Логика событий — test_worker.py."""

import logging
import time

import pytest
from fastapi.testclient import TestClient

from conftest import post_orbita, run, wait_for
from first_contact import config
from first_contact.app import create_app
from first_contact.worker import Worker

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


def test_orbita_event_failed_after_ack_processed_after_restart(webhook_app, waiting_deal, settings, bitrix, orbita,
                                                              monkeypatch):
    """2xx = доставлено, повтора не будет: принятое событие не теряется ни при сбое обработки, ни при перезапуске."""
    original, calls = waiting_deal.on_orbita_event, []

    async def broken(ev):
        calls.append(ev)
        raise RuntimeError("сбой")
    monkeypatch.setattr(waiting_deal, "on_orbita_event", broken)
    with TestClient(webhook_app) as c:
        assert post_orbita(c, {"id": 50, **RECEIVED}, event_id="50").status_code == 200
        wait_for(lambda: calls)
    monkeypatch.setattr(waiting_deal, "on_orbita_event", original)

    restarted = Worker(config.env(), settings, waiting_deal.db, bitrix, orbita)
    run(restarted.tick())
    assert restarted.db.deal(10)["status"] == "replied"


def test_orbita_redelivery_of_accepted_event_is_duplicate(client, waiting_deal):
    post_orbita(client, {"id": 51, **RECEIVED}, event_id="51")
    wait_for(lambda: waiting_deal.db.deal(10)["status"] == "replied")
    assert post_orbita(client, {"id": 51, **RECEIVED}, event_id="51").json().get("duplicate")


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


def test_client_reply_before_send_confirmation_counts(webhook_app, running, bitrix, orbita):
    """202 на первое сообщение; клиент ответил раньше, чем пришло message.sent (номер диалога ещё не знали)."""
    orbita.answers["whatsapp"] = "pending"
    bitrix.new_ids.append(180)
    run(running.tick())
    row = running.db.deal(180)
    assert row["status"] == "pending_first"

    with TestClient(webhook_app) as c:
        post_orbita(c, {"id": 60, "type": "message.received",
                        "data": {"dialog_id": 777, "direction": "incoming", "channel": "whatsapp"}}, event_id="60")
        post_orbita(c, {"id": 61, "type": "message.sent",
                        "data": {"dialog_id": 777, "direction": "outgoing", "request_key": row["request_key"]}},
                    event_id="61")
        wait_for(lambda: running.db.deal(180)["status"] != "pending_first")
    row = running.db.deal(180)
    assert row["status"] == "replied" and row["client_replied_at"]


def test_orbita_webhook_not_ascii_signature_401(webhook_app):
    ts = str(int(time.time()))
    r = TestClient(webhook_app).post("/orbita/webhook", content=b"{}", headers={
        "X-Orbita-Timestamp": ts, "X-Orbita-Signature": "v1=\xff\xfe".encode("latin-1")})
    assert r.status_code == 401


def test_bitrix_event_processing_failure_logged(client, worker, monkeypatch, caplog):
    """Событие Битрикса принято (200), обработка в фоне упала — ошибка в логе с трассировкой и в журнале сделки."""
    worker.s.bitrix_app_token = "T"

    async def broken(deal_id):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(worker, "on_deal_added", broken)
    with caplog.at_level(logging.ERROR, logger="first_contact"):
        r = client.post("/bitrix/deal-add", data={"auth[application_token]": "T", "event": "ONCRMDEALADD",
                                                  "data[FIELDS][ID]": "5"})
        assert r.status_code == 200
        wait_for(lambda: worker.db.events(5))
    assert any(rec.name == "first_contact" and rec.levelno == logging.ERROR and rec.exc_info for rec in caplog.records)
