"""Первое сообщение: логика робота (worker.py) вместе с настоящим клиентом Орбиты (orbita_api.py)
на подменённом HTTP-транспорте — повторы после неизвестного исхода и непроверенные каналы."""

import json

import httpx
import pytest

from conftest import PHONE, run
from first_contact import config
from first_contact.orbita_api import OrbitaAPI
from first_contact.worker import Worker

URL = "https://x/api/public/v1"


class Orbita:
    """Орбита по спецификации: каналы подключены по connected, на POST /dialogs отвечает answers[канал]
    (201, 422-код или "lost" — сообщение ушло, а ответ потерян). posts — (канал, Idempotency-Key)."""

    def __init__(self, connected, answers, invalid_phones=()):
        self.connected, self.answers, self.posts = set(connected), dict(answers), []
        self.invalid_phones = set(invalid_phones)       # на них 400 invalid_request

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{"provider": c, "is_connected": c in self.connected, "id": i}
                                                       for i, c in enumerate(config.CHANNELS)]})
        if req.url.path.endswith("/dialogs") and req.method == "POST":
            body = json.loads(req.content)
            ch = body["channel"]
            self.posts.append((ch, req.headers["idempotency-key"]))
            if body["phone"] in self.invalid_phones:
                return httpx.Response(400, json={"detail": {"code": "invalid_request"}})
            answer = self.answers[ch]
            if isinstance(answer, httpx.Response):
                return answer
            if answer == "lost":
                self.answers[ch] = 201              # повтор с тем же ключом вернёт исход первого вызова
                raise httpx.ReadTimeout("ответ не дошёл")
            if isinstance(answer, str):
                return httpx.Response(422, json={"detail": {"code": answer}})
            return httpx.Response(201, json={"dialog": {"id": 501}, "message_id": 1})
        return httpx.Response(404, json={"detail": {"code": "not_found"}})


@pytest.fixture
def robot(db, settings, bitrix, http):
    def make(orbita: Orbita, channels):
        http(orbita)
        settings.orbita_channels = list(channels)
        w = Worker(config.env(), settings, db, bitrix, OrbitaAPI(URL, "k", list(channels)))
        run(w.enable())
        return w
    return make


@pytest.mark.parametrize("change", ["disconnected", "reordered"])
def test_retry_after_lost_answer_never_goes_to_another_channel(robot, bitrix, change, clock):
    """WhatsApp отправил, ответ потерян; к повтору WhatsApp отключился или порядок каналов сменили."""
    orbita = Orbita({"whatsapp", "max"}, {"whatsapp": "lost", "max": 201})
    w = robot(orbita, ["whatsapp", "max"])
    bitrix.new_ids.append(140)
    run(w.tick())
    assert w.db.deal(140)["status"] == "new"

    if change == "disconnected":
        orbita.connected = {"max"}
    else:
        w.s.orbita_channels = ["max", "whatsapp"]            # как после сохранения в панели (reconnect)
        w.orbita = OrbitaAPI(URL, "k", ["max", "whatsapp"])
    clock.advance(minutes=3)
    run(w.tick())

    assert [ch for ch, _ in orbita.posts] == ["whatsapp"] * len(orbita.posts), "в Max второе первое не ушло"
    expected = "needs_check" if change == "disconnected" else "sent_first"
    assert w.db.deal(140)["status"] == expected
    assert len({key for _, key in orbita.posts}) == 1, "повтор — с тем же ключом"


def test_disconnected_selected_channel_is_not_checked(robot, bitrix):
    """Max ответил «не найден», а выбранный WhatsApp отключён — номер не проверен, это не «нет мессенджера»."""
    w = robot(Orbita({"max"}, {"max": "max_not_found"}), ["whatsapp", "max"])
    bitrix.new_ids.append(141)
    run(w.tick())
    assert w.db.deal(141)["status"] == "new"
    assert bitrix.stages_set == []


def test_not_found_in_all_selected_connected_channels_is_no_messenger(robot, bitrix):
    w = robot(Orbita({"whatsapp", "max"}, {"whatsapp": "not_on_whatsapp", "max": "max_not_found"}),
              ["whatsapp", "max"])
    bitrix.new_ids.append(142)
    run(w.tick())
    assert w.db.deal(142)["status"] == "no_messenger"
    assert bitrix.stages_set == [(142, "NO_MESSENGER")]


OTHER = "+79990000002"


def test_invalid_number_skipped_next_number_tried(robot, bitrix):
    bitrix.phones = [PHONE, OTHER]
    orbita = Orbita({"whatsapp"}, {"whatsapp": 201}, invalid_phones={PHONE})
    w = robot(orbita, ["whatsapp"])
    bitrix.new_ids.append(143)
    run(w.tick())
    row = w.db.deal(143)
    assert (row["status"], row["phone"]) == ("sent_first", OTHER)


def test_all_numbers_invalid_is_no_phone(robot, bitrix):
    bitrix.phones = [PHONE, OTHER]
    w = robot(Orbita({"whatsapp", "max"}, {"whatsapp": 201, "max": 201}, invalid_phones={PHONE, OTHER}),
              ["whatsapp", "max"])
    bitrix.new_ids.append(144)
    run(w.tick())
    assert w.db.deal(144)["status"] == "no_phone"
    assert bitrix.stages_set == [(144, "NO_MESSENGER")]


@pytest.mark.parametrize("answer", [
    httpx.Response(429, headers={"Retry-After": "120"}, json={"detail": {"code": "rate_limited"}}),
    httpx.Response(422, json={"detail": {"code": "daily_send_limit_exhausted"}}),
    httpx.Response(422, headers={"Retry-After": "5"}, json={"detail": {"code": "idempotency_in_progress"}}),
])
def test_known_wait_does_not_spend_attempts(robot, bitrix, answer):
    orbita = Orbita({"whatsapp"}, {"whatsapp": answer})
    w = robot(orbita, ["whatsapp"])
    bitrix.new_ids.append(145)
    run(w.tick())
    run(w.tick())
    row = w.db.deal(145)
    assert (row["status"], row["attempts"]) == ("new", 0)
    assert len(orbita.posts) == 1, "до срока Орбиту не дёргаем"


def test_telegram_flood_wait_postpones_without_attempt(robot, bitrix, clock):
    """WhatsApp: «не найден», Telegram: подождите retry_after — сделка ждёт, попытка не тратится."""
    flood = httpx.Response(422, json={"detail": {"code": "telegram_flood_wait", "retry_after": 300}})
    orbita = Orbita({"whatsapp", "telegram"}, {"whatsapp": "not_on_whatsapp", "telegram": flood})
    w = robot(orbita, ["whatsapp", "telegram"])
    bitrix.new_ids.append(146)
    run(w.tick())
    run(w.tick())
    row = w.db.deal(146)
    assert (row["status"], row["attempts"]) == ("new", 0)
    assert len(orbita.posts) == 2
    clock.advance(seconds=301)
    run(w.tick())
    assert len(orbita.posts) == 4, "срок прошёл — каналы проверяются снова"
