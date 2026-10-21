"""Клиент REST API Орбиты (orbita_api.py) на httpx.MockTransport по спецификации docs/orbita-openapi-*.json."""

import json
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from conftest import UTC, run, sign
from first_contact import orbita_api
from first_contact.orbita import OrbitaError, client_replied
from first_contact.orbita_api import OrbitaAPI, OrbitaAPIError, verify_signature

SPEC = Path(__file__).resolve().parents[1] / "docs" / "orbita-openapi-2026-10-20.json"
URL = "https://x/api/public/v1"
ALL = ["whatsapp", "max", "telegram"]
CONNECTED = (("whatsapp", True), ("telegram", False), ("max", True))


def not_found(code):
    return 422, {"detail": {"code": code}}


def orbita(dialogs=None, channels=CONNECTED):
    """Обработчик по спецификации: /channels, POST /dialogs (ответ по каналу), история, ответ в диалог."""
    dialogs = dialogs or {}

    def handler(req):
        path = req.url.path
        if path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{"provider": p, "is_connected": c, "id": i}
                                                       for i, (p, c) in enumerate(channels)]})
        if path.endswith("/dialogs") and req.method == "POST":
            status, body = dialogs[json.loads(req.content)["channel"]]
            return httpx.Response(status, json=body)
        if path.endswith("/messages") and req.method == "GET":
            return httpx.Response(200, json={"items": [
                {"direction": "incoming", "sent_at": "2026-10-20T10:10:00+00:00"},
                {"direction": "outgoing", "sent_at": "2026-10-20T10:00:00+00:00"}]})
        if path.endswith("/messages") and req.method == "POST":
            return httpx.Response(202, json={"request_key": "rk", "message_id": 5})
        return httpx.Response(404, json={"detail": {"code": "not_found"}})
    return handler


def tried_channels(calls):
    return [c[3]["channel"] for c in calls if c[0] == "POST" and c[1].endswith("/dialogs")]


# ---------- «написать первым» ----------

def test_send_first_201_sent(http):
    calls = http(orbita({"whatsapp": (201, {"dialog": {"id": 501}, "message_id": 1})}))
    r = run(OrbitaAPI(URL, "k", ALL).send_first("+79990000001", "привет", "deal-1-first-+7999", "Анна"))
    assert (r.status, r.dialog_id, r.channel) == ("sent", "501", "whatsapp")
    method, path, key, body, _ = calls[-1]
    assert (method, path, key) == ("POST", "/api/public/v1/dialogs", "deal-1-first-+7999-whatsapp")
    assert body == {"channel": "whatsapp", "phone": "+79990000001", "text": "привет", "name": "Анна"}
    assert {c[4] for c in calls} == {"k"}, "ключ API в каждом запросе"


def test_not_found_tries_next_connected_channel(http):
    calls = http(orbita({"whatsapp": not_found("not_on_whatsapp"), "max": (201, {"dialog": {"id": 7}})}))
    r = run(OrbitaAPI(URL, "k", ALL).send_first("+7", "t", "k1"))
    assert (r.status, r.channel) == ("sent", "max")
    assert tried_channels(calls) == ["whatsapp", "max"], "telegram не подключён — не трогаем"


def test_channel_order_from_settings(http):
    calls = http(orbita({"max": not_found("max_not_found"), "whatsapp": (201, {"dialog": {"id": 8}})}))
    r = run(OrbitaAPI(URL, "k", ["max", "whatsapp"]).send_first("+7", "t", "k-order"))
    assert tried_channels(calls) == ["max", "whatsapp"] and r.channel == "whatsapp"


@pytest.mark.parametrize("channel, code", [
    ("whatsapp", "not_on_whatsapp"),
    ("max", "max_not_found"),
    ("telegram", "telegram_recipient_not_found"),
    ("telegram", "telegram_recipient_refused"),
])
def test_number_not_in_messenger_codes(http, channel, code):
    http(orbita({channel: not_found(code)}, channels=[(channel, True)]))
    assert run(OrbitaAPI(URL, "k", [channel]).send_first("+7", "t", "k2")).status == "no_messenger"


def test_not_found_everywhere_is_no_messenger(http):
    """«Не найден» во всех выбранных каналах (telegram не выбран — он не подключён)."""
    http(orbita({"whatsapp": not_found("not_on_whatsapp"), "max": not_found("max_not_found")}))
    assert run(OrbitaAPI(URL, "k", ["whatsapp", "max"]).send_first("+7", "t", "k2")).status == "no_messenger"


def test_unchecked_channel_is_error_not_no_messenger(http):
    """Хоть один канал не смог проверить номер — ошибка с повтором, а не «нет мессенджера»."""
    http(orbita({"whatsapp": not_found("not_connected"), "max": not_found("max_not_found")}))
    with pytest.raises(OrbitaError):
        run(OrbitaAPI(URL, "k", ALL).send_first("+7", "t", "k3"))


def test_key_level_refusal_raised_without_trying_other_channels(http):
    calls = http(orbita({"whatsapp": not_found("daily_send_limit_exhausted")}))
    with pytest.raises(OrbitaAPIError) as exc:
        run(OrbitaAPI(URL, "k", ALL).send_first("+7", "t", "k4"))
    assert exc.value.code == "daily_send_limit_exhausted"
    assert tried_channels(calls) == ["whatsapp"]


def test_no_connected_channels_is_error(http):
    calls = http(orbita(channels=[("whatsapp", False)]))
    with pytest.raises(OrbitaError):
        run(OrbitaAPI(URL, "k", ALL).send_first("+7", "t", "k5"))
    assert tried_channels(calls) == []


def test_202_is_pending_with_request_key(http):
    http(orbita({"whatsapp": (202, {"request_key": "rk-1"})}))
    r = run(OrbitaAPI(URL, "k", ALL).send_first("+7", "t", "k6"))
    assert (r.status, r.request_key) == ("pending", "rk-1")


def test_201_without_dialog_gives_empty_dialog_id(http):
    http(orbita({"whatsapp": (201, {"message_id": 1})}))
    r = run(OrbitaAPI(URL, "k", ALL).send_first("+7", "t", "k-nodialog"))
    assert r.status == "sent" and r.dialog_id is None


def test_connection_failure_is_retryable_orbita_error(http):
    def boom(req):
        raise httpx.ConnectTimeout("t")
    http(boom)
    with pytest.raises(OrbitaError):
        run(OrbitaAPI(URL, "k", ALL).send_first("+7", "t", "k7"))


def test_error_body_not_object_still_api_error(http):
    http(lambda req: httpx.Response(502, json=["bad gateway"]))
    with pytest.raises(OrbitaAPIError) as exc:
        run(OrbitaAPI(URL, "k", ALL).account())
    assert exc.value.status == 502


# ---------- каналы, история, ответ ----------

def test_channel_status_all_three_messengers(http):
    http(orbita(channels=[("whatsapp", False), ("whatsapp", True), ("max", False)]))
    assert run(OrbitaAPI(URL, "k", ALL).channel_status()) == {"whatsapp": True, "max": False, "telegram": None}


def test_history_directions_and_client_reply(http):
    http(orbita())
    msgs = run(OrbitaAPI(URL, "k", ALL).history("501"))
    assert [m.outgoing for m in msgs] == [False, True]
    assert client_replied(msgs, datetime(2026, 10, 20, 10, 0, tzinfo=UTC)) is True
    assert client_replied(msgs, datetime(2026, 10, 20, 10, 30, tzinfo=UTC)) is None, "старая реплика — не ответ"


def paged_history(pages):
    """История по страницам (новые первыми): pages — списки сообщений; курсор следующей — её номер."""
    def handler(req):
        n = int(req.url.params.get("cursor") or 0)
        nxt = str(n + 1) if n + 1 < len(pages) else None
        return httpx.Response(200, json={"items": pages[n], "next_cursor": nxt})
    return handler


def m(direction, sent_at):
    return {"id": 1, "dialog_id": 501, "channel": "whatsapp", "direction": direction, "sent_at": sent_at}


def test_history_follows_next_cursor(http):
    """Входящий ответ клиента — на второй странице, первая забита исходящими."""
    calls = http(paged_history([[m("outgoing", "2026-10-20T12:00:00+00:00")] * 3,
                                [m("incoming", "2026-10-20T10:10:00+00:00"),
                                 m("outgoing", "2026-10-20T10:00:00+00:00")]]))
    msgs = run(OrbitaAPI(URL, "k", ALL).history("501"))
    assert client_replied(msgs, datetime(2026, 10, 20, 10, 0, tzinfo=UTC)) is True
    assert len([c for c in calls if c[1].endswith("/messages")]) == 2


def test_history_stops_at_since(http):
    """Страницы старше нашего первого сообщения (с запасом на часы) не читаются."""
    calls = http(paged_history([[m("incoming", "2026-10-20T10:10:00+00:00"),
                                 m("outgoing", "2026-10-20T09:00:00+00:00")],
                                [m("incoming", "2026-10-19T10:00:00+00:00")]]))
    run(OrbitaAPI(URL, "k", ALL).history("501", datetime(2026, 10, 20, 10, 0, tzinfo=UTC)))
    assert len([c for c in calls if c[1].endswith("/messages")]) == 1


def test_history_time_without_offset_is_utc(http):
    http(paged_history([[m("incoming", "2026-10-20T10:10:00")]]))
    (msg,) = run(OrbitaAPI(URL, "k", ALL).history("501"))
    assert msg.at == datetime(2026, 10, 20, 10, 10, tzinfo=UTC)


def test_send_reply_202_pending_with_request_key(http):
    calls = http(orbita())
    r = run(OrbitaAPI(URL, "k", ALL).send_reply("501", "второе", "k8"))
    assert (r.status, r.request_key) == ("pending", "rk"), "202 — команда передана, исход придёт вебхуком"
    assert calls[-1][:4] == ("POST", "/api/public/v1/dialogs/501/messages", "k8", {"text": "второе"})


def test_send_reply_201_sent(http):
    http(lambda req: httpx.Response(201, json={"id": 9, "dialog_id": 501, "channel": "telegram",
                                               "direction": "outgoing"}))
    assert run(OrbitaAPI(URL, "k", ALL).send_reply("501", "второе", "k8")).status == "sent"


# ---------- подпись вебхука ----------

@pytest.fixture(scope="module")
def spec_example():
    return json.loads(SPEC.read_text(encoding="utf-8"))["info"]["x-orbita-signature-example"]


def test_signature_example_from_spec(spec_example):
    s = spec_example
    assert verify_signature(s["secret"], s["timestamp"], s["body"].encode(), s["signature"], max_skew=0)


def test_signature_tampered_body_rejected(spec_example):
    s = spec_example
    assert not verify_signature(s["secret"], s["timestamp"], s["body"].encode() + b" ", s["signature"], max_skew=0)


def test_signature_stale_timestamp_rejected(spec_example):
    s = spec_example
    assert not verify_signature(s["secret"], s["timestamp"], s["body"].encode(), s["signature"])


def test_signature_one_of_several_matches():
    ts, body = str(int(time.time())), b'{"id":1}'
    good = sign("whsec", ts, body)
    assert verify_signature("whsec", ts, body, f"v1=00ff, {good}")
    assert not verify_signature("whsec", ts, body, "v1=00ff, v1=11ee")


@pytest.mark.parametrize("secret, timestamp, signature", [
    ("", "now", "good"),            # секрет не задан — вебхуки не принимаются
    ("whsec", "", "good"),
    ("whsec", "now", ""),
    ("whsec", "abc", "good"),       # нечисловой timestamp
    ("whsec", "old", "good"),       # старше max_skew
])
def test_signature_rejected(secret, timestamp, signature):
    now = str(int(time.time()))
    ts = {"now": now, "old": str(int(time.time()) - 301)}.get(timestamp, timestamp)
    body = b'{"id":1}'
    sig = sign(secret or "whsec", ts, body) if signature == "good" else signature
    assert not verify_signature(secret, ts, body, sig)


def test_disconnected_selected_channel_makes_send_first_unchecked(http):
    """Выбранный, но отключённый канал не проверял номер — «нет мессенджера» утверждать нельзя."""
    http(orbita({"max": not_found("max_not_found")}, channels=[("whatsapp", False), ("max", True)]))
    with pytest.raises(OrbitaError):
        run(OrbitaAPI(URL, "k", ["whatsapp", "max"]).send_first("+7", "t", "k9"))


@pytest.mark.parametrize("signature", ["v1=ÿÿ", "v1=подпись", "v1=ab"])
def test_signature_not_ascii_or_other_length_rejected(signature):
    ts = str(int(time.time()))
    assert verify_signature("whsec", ts, b'{"id":1}', signature) is False
