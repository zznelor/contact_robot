"""Клиент REST API Орбиты (orbita_api.py) на httpx.MockTransport по спецификации docs/orbita-openapi-*.json."""

import json
import time
from datetime import datetime, timedelta, timezone
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
    assert [(x.outgoing, x.at.hour, x.at.minute) for x in msgs] == [(True, 12, 0)] * 3 + [(False, 10, 10), (True, 10, 0)]


def test_history_stops_at_since(http):
    """Страницы старше нашего первого сообщения (с запасом на часы) не читаются."""
    calls = http(paged_history([[m("incoming", "2026-10-20T10:10:00+00:00"),
                                 m("outgoing", "2026-10-20T09:00:00+00:00")],
                                [m("incoming", "2026-10-19T10:00:00+00:00")]]))
    run(OrbitaAPI(URL, "k", ALL).history("501", datetime(2026, 10, 20, 10, 0, tzinfo=UTC)))
    assert len([c for c in calls if c[1].endswith("/messages")]) == 1


@pytest.mark.parametrize("sent_at", ["2026-10-20T10:10:00", "2026-10-20T13:10:00+03:00", "2026-10-20T10:10:00Z"])
def test_history_time_without_offset_is_utc(http, sent_at):
    """sent_at — date-time: со смещением — переводится, без смещения — UTC."""
    http(paged_history([[m("incoming", sent_at)]]))
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
    ("", "now", "good"),            # секрет не задан — вебхуки не принимаются, даже подписанные пустым секретом
    ("whsec", "", "good"),
    ("whsec", "now", ""),
    ("whsec", "abc", "good"),       # нечисловой timestamp
    ("whsec", "old", "good"),       # старше max_skew
])
def test_signature_rejected(secret, timestamp, signature):
    now = str(int(time.time()))
    ts = {"now": now, "old": str(int(time.time()) - 301)}.get(timestamp, timestamp)
    body = b'{"id":1}'
    sig = sign(secret, ts, body) if signature == "good" else signature
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


# ---------- адрес, ключ и таймаут запроса ----------

def recorded(answer):
    """Обработчик транспорта: reqs — запросы целиком (адрес, заголовки, таймаут)."""
    reqs = []

    def handler(req):
        reqs.append(req)
        return answer(req)
    return reqs, handler


@pytest.fixture(scope="module")
def spec():
    return json.loads(SPEC.read_text(encoding="utf-8"))


def example(spec, path: str) -> dict:
    return spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["example"]


@pytest.mark.parametrize("url", [URL, URL + "/"])
def test_account_by_spec(http, spec, url):
    """GET /account с ключом в X-Api-Key — объект аккаунта (пример ответа спецификации); адрес API из настроек — со
    слешем на конце и без."""
    account = example(spec, "/account")
    reqs, handler = recorded(lambda req: httpx.Response(200, json=account))
    http(handler)
    assert run(OrbitaAPI(url, "k", ALL).account()) == account
    (req,) = reqs
    assert (req.method, str(req.url), req.headers["x-api-key"]) == ("GET", "https://x/api/public/v1/account", "k")


def test_timeout_from_settings_on_every_request(http):
    """Таймаут клиента доходит до запроса: Орбита, которая не отвечает, не держит проход робота бесконечно."""
    reqs, handler = recorded(lambda req: httpx.Response(200, json={"items": []}))
    http(handler)
    run(OrbitaAPI(URL, "k", ALL, timeout=7).channels())
    assert reqs[0].extensions["timeout"] == {"connect": 7, "read": 7, "write": 7, "pool": 7}


def test_channels_by_spec(http, spec):
    """GET /channels (пример ответа спецификации): мессенджеры робота, которых в Орбите нет, — «не подключали»."""
    reqs, handler = recorded(lambda req: httpx.Response(200, json=example(spec, "/channels")))
    http(handler)
    assert run(OrbitaAPI(URL, "k", ALL).channel_status()) == {"whatsapp": True, "max": None, "telegram": None}
    assert [(r.method, r.url.path) for r in reqs] == [("GET", "/api/public/v1/channels")]


@pytest.mark.parametrize("channels", [[("whatsapp", True), ("whatsapp", False)],
                                      [("whatsapp", False), ("whatsapp", True)]])
def test_channel_connected_if_any_employee_connected(http, channels):
    """Мессенджер подключён, если подключён хоть у одного сотрудника — в каком бы порядке /channels их ни отдал."""
    http(orbita(channels=channels))
    assert run(OrbitaAPI(URL, "k", ALL).channel_status())["whatsapp"] is True


# ---------- «написать первым» в один канал: исход по ответу POST /dialogs ----------

@pytest.mark.parametrize("answer, expected", [
    # 201 — {"dialog": Dialog, "message_id": int}; 202 — {"request_key": "…"} (спецификация POST /dialogs)
    ((201, {"dialog": {"id": 501, "channel": "whatsapp"}, "message_id": 90211}), ("sent", "501", "whatsapp", None)),
    ((202, {"request_key": "3f9c2a"}), ("pending", None, "whatsapp", "3f9c2a")),
])
def test_start_dialog_names_channel_it_went_to(http, answer, expected):
    """Ушло (201) или могло уйти (202): результат называет канал — робот записывает его в сделку, — диалог и
    request_key, по которому придёт исход."""
    http(orbita({"whatsapp": answer}))
    r = run(OrbitaAPI(URL, "k", ALL).start_dialog("whatsapp", "+79990000001", "t", "deal-1-first-+7-whatsapp"))
    assert (r.status, r.dialog_id, r.channel, r.request_key) == expected


AT = datetime(2026, 10, 20, 22, 0, tzinfo=UTC)


def frozen(monkeypatch, at: datetime = AT, local=timezone(timedelta(hours=5))) -> None:
    """Часы клиента Орбиты стоят на at; часы машины без зоны — в local (здесь UTC+5: там уже следующие сутки)."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz) if tz else at.astimezone(local).replace(tzinfo=None)
    monkeypatch.setattr(orbita_api, "datetime", Clock)


@pytest.mark.parametrize("detail, wait", [
    ({"code": "not_connected"}, None),                                  # WhatsApp и Max: канал не подключён
    ({"code": "telegram_flood_wait", "retry_after": 30}, timedelta(seconds=30)),   # + retry_after, с
])
def test_start_dialog_unchecked_keeps_refusal_code_and_wait(http, monkeypatch, detail, wait):
    """422 канала, который не проверил номер: «не проверен» с кодом отказа (робот показывает его человеку) и сроком
    retry_after, если Орбита его назвала."""
    frozen(monkeypatch)
    http(lambda req: httpx.Response(422, json={"detail": detail}))
    r = run(OrbitaAPI(URL, "k", ALL).start_dialog("telegram", "+79990000001", "t", "k-unchecked"))
    assert (r.status, r.raw, r.retry_at) == ("unchecked", detail["code"], AT + wait if wait else None)


def test_start_dialog_server_error_is_unknown_outcome(http):
    """5xx на POST /dialogs: исход неизвестен — исключение без «точно не отправлено» (definite), а не «канал не
    проверил номер»: иначе робот пошёл бы в следующий канал, и клиент мог получить два первых сообщения."""
    http(lambda req: httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(OrbitaAPIError) as e:
        run(OrbitaAPI(URL, "k", ALL).start_dialog("whatsapp", "+79990000001", "t", "k-500"))
    assert e.value.status == 500 and not e.value.definite


@pytest.mark.parametrize("order, channels, dialogs, sent_to", [
    # whatsapp не смог проверить номер (not_connected) — следующий выбранный канал
    (ALL, CONNECTED, {"whatsapp": not_found("not_connected"), "max": (201, {"dialog": {"id": 7}})}, "max"),
    # первый выбранный канал не подключён — следующий
    (["telegram", "whatsapp"], [("telegram", False), ("whatsapp", True)], {"whatsapp": (201, {"dialog": {"id": 7}})},
     "whatsapp"),
])
def test_send_first_goes_on_to_next_channel(http, order, channels, dialogs, sent_to):
    http(orbita(dialogs, channels=channels))
    r = run(OrbitaAPI(URL, "k", order).send_first("+79990000001", "t", "k-next"))
    assert (r.status, r.channel) == ("sent", sent_to)


def test_send_first_invalid_phone_stops_at_first_channel(http):
    """400 на POST /dialogs — «невалидный номер»: он невалиден для любого канала, следующие не пробуются."""
    calls = http(orbita({"whatsapp": (400, {"detail": {"code": "invalid_request"}}), "max": (201, {"dialog": {"id": 7}})}))
    with pytest.raises(OrbitaError):
        run(OrbitaAPI(URL, "k", ALL).send_first("+7", "t", "k-invalid"))
    assert tried_channels(calls) == ["whatsapp"]


def test_send_first_without_selected_channels_is_error(http):
    """Ни одного выбранного мессенджера — ошибка, а не «номера нет ни в одном из выбранных»."""
    http(orbita())
    with pytest.raises(OrbitaError):
        run(OrbitaAPI(URL, "k", []).send_first("+79990000001", "t", "k-none"))


# ---------- сроки, названные Орбитой ----------

def test_daily_limit_waits_until_utc_midnight(http, monkeypatch):
    """daily_send_limit_exhausted — «1000 отправок в сутки UTC»: срок — ближайшая полночь UTC, со смещением, а не
    полночь по часам машины (в UTC+5 там уже следующие сутки)."""
    frozen(monkeypatch)
    http(orbita({"whatsapp": not_found("daily_send_limit_exhausted")}))
    with pytest.raises(OrbitaAPIError) as e:
        run(OrbitaAPI(URL, "k", ALL).start_dialog("whatsapp", "+79990000001", "t", "k-daily"))
    assert e.value.retry_at == datetime(2026, 10, 21, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("seconds", [1, 120])
def test_rate_limit_waits_retry_after(http, monkeypatch, seconds):
    """429 rate_limited с Retry-After («Через сколько секунд повторить», minimum 1): срок — через столько секунд."""
    frozen(monkeypatch)
    http(lambda req: httpx.Response(429, headers={"Retry-After": str(seconds)}, json={"detail": {"code": "rate_limited"}}))
    with pytest.raises(OrbitaAPIError) as e:
        run(OrbitaAPI(URL, "k", ALL).account())
    assert e.value.retry_at == AT + timedelta(seconds=seconds)


# ---------- история: страницы до since и конец ленты ----------

@pytest.mark.parametrize("first_page_at", ["2026-10-20T09:59:00+00:00", "2026-10-20T09:58:00+00:00"])
def test_history_reads_next_page_within_clock_skew(http, first_page_at):
    """Первая страница не старше нашего первого сообщения с запасом на часы (2 минуты, включительно) — следующая
    страница тоже читается: на ней ответ клиента, который часы Орбиты записали чуть раньше."""
    since = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    calls = http(paged_history([[m("outgoing", first_page_at)], [m("incoming", "2026-10-20T09:58:00+00:00")]]))
    msgs = run(OrbitaAPI(URL, "k", ALL).history("501", since))
    assert len([c for c in calls if c[1].endswith("/messages")]) == 2
    assert client_replied(msgs, since) is True


def test_history_repeated_cursor_ends_feed(http):
    """Орбита вернула курсор, который уже был: лента кончилась — иначе чтение истории не закончилось бы."""
    reqs = []

    def handler(req):
        reqs.append(req)
        if len(reqs) > 5:
            raise AssertionError("история читается без конца")
        return httpx.Response(200, json={"items": [m("outgoing", "2026-10-20T12:00:00+00:00")], "next_cursor": "c1"})
    http(handler)
    assert len(run(OrbitaAPI(URL, "k", ALL).history("501"))) == 2
    assert [r.url.params.get("cursor") for r in reqs] == [None, "c1"]
