"""Логика робота (worker.py) на подделках Битрикса и Орбиты. Эталон — docs/РАЗРАБОТЧИКУ.md."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from conftest import PHONE, UTC, Gate, WatchedLock, ago, async_panel, first_of, run, start_deal
from first_contact import config
from first_contact import worker as worker_mod
from first_contact.orbita import Message
from first_contact.worker import render


def out(at):
    return Message("", "", at, True)


def inc(at):
    return Message("", "", at, False)


def event(kind, **data):
    return {"type": kind, "data": data}


# ---------- включение и выключение ----------

def test_disabled_robot_does_nothing(worker, bitrix, orbita):
    bitrix.new_ids = [101]
    run(worker.tick())
    run(worker.on_deal_added(101))
    assert worker.db.get("last_deal_id") is None
    assert worker.db.deal(101) is None
    assert orbita.keys == []


def test_enable_remembers_last_deal_and_skips_older(worker, bitrix, orbita):
    bitrix.max_id = 101
    run(worker.enable())
    assert worker.s.enabled and worker.db.get("last_deal_id") == "101"
    assert config.load(worker.db).enabled, "флаг «включён» сохранён в базе"

    bitrix.new_ids = [101, 102]
    run(worker.tick())
    assert worker.db.deal(101) is None
    assert worker.db.deal(102)["status"] == "sent_first"


def test_disable_stops_taking_new_deals(running, bitrix):
    run(running.disable())
    bitrix.new_ids = [110]
    run(running.tick())
    assert running.db.deal(110) is None
    assert not config.load(running.db).enabled


# ---------- первое сообщение ----------

def test_first_message_substitutes_name_and_title(running, bitrix, orbita):
    running.s.tpl_first = "{name} / {deal_title}"
    row = start_deal(running, bitrix, 102)
    assert row["status"] == "sent_first"
    assert orbita.first == [(PHONE, "Анна / Заявка", f"deal-102-first-{PHONE}")]


@pytest.mark.parametrize("deal_category_id, deal_category, expected", [
    ("0", "5", "skipped"),       # другая воронка
    (None, "5", "skipped"),      # воронка не выбрана → основная «0», как в панели
    (None, "0", "sent_first"),
    ("3", "3", "sent_first"),
])
def test_only_selected_funnel(running, bitrix, deal_category_id, deal_category, expected):
    running.s.deal_category_id = deal_category_id
    bitrix.category = deal_category
    assert start_deal(running, bitrix, 109)["status"] == expected


def test_no_messenger_moves_deal_to_stage(running, bitrix, orbita):
    orbita.mode = "none"
    assert start_deal(running, bitrix, 120)["status"] == "no_messenger"
    assert bitrix.stages_set == [(120, "NO_MESSENGER")]


def test_deal_on_no_messenger_stage_untouched_without_manual_retry(running, bitrix, orbita):
    bitrix.stage = "NO_MESSENGER"
    assert start_deal(running, bitrix, 121)["status"] == "skipped"
    assert orbita.keys == []


# ---------- шаги по одному (кнопка в панели) ----------

def test_steps_one_by_one(running, bitrix, orbita):
    bitrix.new_ids = [111]
    run(running.run_step("poll"))
    assert running.db.deal(111)["status"] == "new" and orbita.keys == [], "«забрать сделки» только ставит в очередь"
    run(running.run_step("first"))
    assert running.db.deal(111)["status"] == "sent_first" and len(orbita.first) == 1


def test_unknown_step_rejected(running):
    with pytest.raises(KeyError):
        run(running.run_step("oops"))


# ---------- ошибки и «Повторить» ----------

def test_failures_count_and_reset_when_deal_moves(running, bitrix, orbita):
    orbita.mode = "boom"
    bitrix.new_ids = [122]
    for _ in range(4):
        run(running.tick())
    row = running.db.deal(122)
    assert (row["status"], row["attempts"]) == ("new", 4)

    orbita.mode = "sent"
    run(running.tick())
    row = running.db.deal(122)
    assert (row["status"], row["attempts"]) == ("sent_first", 0)


def test_five_failures_in_a_row_is_error(running, bitrix, orbita):
    orbita.mode = "boom"
    bitrix.new_ids = [123]
    for _ in range(5):
        run(running.tick())
    assert running.db.deal(123)["status"] == "error"


def test_retry_after_no_messenger_sends_with_new_idempotency_key(running, bitrix, orbita):
    orbita.mode = "none"
    created_at = start_deal(running, bitrix, 120)["created_at"]
    assert bitrix.stage == "NO_MESSENGER"

    orbita.mode = "sent"
    assert run(running.retry(120, "a@x.ru")) == "new"
    run(running.on_deal_added(120))

    row = running.db.deal(120)
    assert row["status"] == "sent_first", "этап «Не нашёл мессенджер» поставил сам робот — после «Повторить» не помеха"
    assert orbita.keys == [f"deal-120-first-{PHONE}", f"deal-120-first-{PHONE}-r1"]
    assert row["created_at"] == created_at and row["retried_at"]


def test_retry_rejected_for_deal_in_progress(running, bitrix):
    start_deal(running, bitrix, 120)
    with pytest.raises(ValueError):
        run(running.retry(120, "a@x.ru"))


def test_retry_after_failed_followup_does_not_resend_first(running, bitrix, orbita):
    start_deal(running, bitrix, 120)
    running.db.update(120, status="error", first_sent_at=ago(hours=1).isoformat(),
                      followup_due_at=ago(minutes=1).isoformat())
    orbita.history_items["D1"] = [out(ago(hours=1))]

    assert run(running.retry(120, "a@x.ru")) == "sent_first"
    run(running.tick())

    assert running.db.deal(120)["status"] == "sent_followup"
    assert len(orbita.first) == 1
    assert [k for _, _, k in orbita.replies] == ["deal-120-followup"]


# ---------- напоминание и итог ----------

@pytest.mark.parametrize("followup_on, expected_status, expected_keys", [
    (False, "no_reply", []),
    (True, "sent_followup", ["deal-106-followup"]),
])
def test_followup_when_client_silent(running, bitrix, orbita, followup_on, expected_status, expected_keys):
    running.s.followup_on = followup_on
    start_deal(running, bitrix, 106)
    running.db.update(106, followup_due_at=ago(minutes=1).isoformat(), first_sent_at=ago(hours=25).isoformat())
    orbita.history_items["D1"] = [out(ago(hours=25))]

    run(running.tick())

    assert running.db.deal(106)["status"] == expected_status
    assert [k for _, _, k in orbita.replies] == expected_keys


def test_followup_without_dialog_needs_check_without_asking_orbita(running, orbita):
    running.db.add_deal(124)
    running.db.update(124, status="sent_first", dialog_id=None, first_sent_at=ago(hours=25).isoformat(),
                      followup_due_at=ago(minutes=1).isoformat())
    run(running.tick())
    assert running.db.deal(124)["status"] == "needs_check"
    assert orbita.history_calls == [] and orbita.replies == []


@pytest.mark.parametrize("followup_sent_hours_ago, history, expected", [
    (25, [out(ago(hours=50))], "no_reply"),                       # молчит и после напоминания
    (25, [out(ago(hours=50)), inc(ago(hours=2))], "replied"),     # ответил после напоминания
    (1, [out(ago(hours=50)), inc(ago(hours=2))], "sent_followup"),  # срок итога не наступил
])
def test_outcome_after_followup(running, orbita, followup_sent_hours_ago, history, expected):
    running.db.add_deal(122)
    running.db.update(122, status="sent_followup", dialog_id="D1", first_sent_at=ago(hours=50).isoformat(),
                      followup_sent_at=ago(hours=followup_sent_hours_ago).isoformat())
    orbita.history_items["D1"] = history
    run(running.tick())
    assert running.db.deal(122)["status"] == expected


# ---------- клиент ответил: ответ по шаблону ----------

@pytest.fixture
def replying(running):
    running.s.reply_on, running.s.reply_delay_minutes = True, 0
    return running


def client_writes(w, bitrix, orbita, deal_id, history):
    """Сделка с ушедшим первым сообщением, клиент написал — вебхук message.received."""
    start_deal(w, bitrix, deal_id)
    orbita.history_items["D1"] = history(datetime.fromisoformat(w.db.deal(deal_id)["first_sent_at"]))
    run(w.on_orbita_event(event("message.received", dialog_id="D1", direction="incoming")))


def test_reply_by_template_sent_once_after_pause(replying, bitrix, orbita):
    client_writes(replying, bitrix, orbita, 102, lambda first: [out(first), inc(datetime.now(UTC))])
    row = replying.db.deal(102)
    assert row["status"] == "replied" and row["reply_due_at"]

    run(replying.tick())
    row = replying.db.deal(102)
    assert [k for _, _, k in orbita.replies] == ["deal-102-reply"]
    assert row["reply_sent_at"] and not row["reply_due_at"]


@pytest.mark.parametrize("again", ["webhook", "retry"])
def test_reply_by_template_not_sent_twice(replying, bitrix, orbita, again):
    client_writes(replying, bitrix, orbita, 102, lambda first: [out(first), inc(datetime.now(UTC))])
    run(replying.tick())
    if again == "webhook":
        run(replying.on_orbita_event(event("message.received", dialog_id="D1", direction="incoming")))
    else:
        replying.db.update(102, status="error")
        assert run(replying.retry(102, "a@x.ru")) == "replied"
    run(replying.tick())
    assert len(orbita.replies) == 1


def test_no_template_reply_when_manager_answered(replying, bitrix, orbita):
    now = datetime.now(UTC)
    client_writes(replying, bitrix, orbita, 103,
                  lambda first: [out(first), inc(now), out(now + timedelta(seconds=30))])
    run(replying.tick())
    assert orbita.replies == []
    assert not replying.db.deal(103)["reply_due_at"]


def test_no_template_reply_when_client_wrote_long_ago(replying, bitrix, orbita):
    start_deal(replying, bitrix, 104)
    replying.db.update(104, first_sent_at=ago(hours=3).isoformat())
    orbita.history_items["D1"] = [out(ago(hours=3)), inc(ago(hours=2))]
    run(replying.on_orbita_event(event("message.received", dialog_id="D1", direction="incoming")))
    run(replying.tick())
    assert orbita.replies == []
    assert not replying.db.deal(104)["reply_due_at"]


def test_reply_by_template_off_not_planned(running, bitrix, orbita):
    client_writes(running, bitrix, orbita, 105, lambda first: [out(first), inc(datetime.now(UTC))])
    row = running.db.deal(105)
    assert row["status"] == "replied" and not row["reply_due_at"]


# ---------- поиск ответа опросом истории (без вебхуков) ----------

def silent_deal_with_reply(w, orbita, checked_at):
    w.db.add_deal(108)
    w.db.update(108, status="sent_first", dialog_id="D1", first_sent_at=ago(minutes=10).isoformat(),
                followup_due_at=ago(hours=-20).isoformat(), checked_at=checked_at)
    orbita.history_items["D1"] = [out(ago(minutes=10)), inc(ago(minutes=1))]


@pytest.mark.parametrize("checked_minutes_ago, expected", [
    (1, "sent_first"),    # смотрели меньше 5 минут назад — не чаще раза в 5 минут
    (6, "replied"),
    (None, "replied"),    # ещё не смотрели
])
def test_poll_replies_without_webhooks(running, orbita, checked_minutes_ago, expected):
    running.s.reply_on, running.s.orbita_webhook_secret = True, ""
    checked = ago(minutes=checked_minutes_ago).isoformat() if checked_minutes_ago is not None else None
    silent_deal_with_reply(running, orbita, checked)
    run(running.tick())
    assert running.db.deal(108)["status"] == expected


def test_poll_replies_not_automatic_with_webhooks(running, orbita):
    running.s.reply_on, running.s.orbita_webhook_secret = True, "whsec"
    silent_deal_with_reply(running, orbita, None)
    run(running.tick())
    assert running.db.deal(108)["status"] == "sent_first"


def test_poll_replies_by_button_ignores_webhooks_and_pause(running, orbita):
    running.s.reply_on, running.s.orbita_webhook_secret = True, "whsec"
    silent_deal_with_reply(running, orbita, ago(minutes=1).isoformat())
    run(running.run_step("replies"))
    assert running.db.deal(108)["status"] == "replied"


# ---------- события Орбиты (подпись и дубли — test_app.py) ----------

@pytest.mark.parametrize("status", ["sent_first", "sent_followup", "no_reply"])
def test_message_received_marks_client_replied(worker, bitrix, status):
    worker.db.add_deal(10)
    worker.db.update(10, status=status, dialog_id="501", phone="+7")
    run(worker.on_orbita_event(event("message.received", dialog_id=501, direction="incoming")))
    row = worker.db.deal(10)
    assert row["status"] == "replied" and row["client_replied_at"]
    assert bitrix.noted(10)


def test_outgoing_message_received_is_not_client_reply(worker):
    worker.db.add_deal(10)
    worker.db.update(10, status="sent_first", dialog_id="501")
    run(worker.on_orbita_event(event("message.received", dialog_id=501, direction="outgoing")))
    assert worker.db.deal(10)["status"] == "sent_first"


def test_message_sent_confirms_pending_first(worker, bitrix):
    worker.db.add_deal(11)
    worker.db.update(11, status="pending_first", request_key="rk-9", channel="whatsapp", phone="+8")
    run(worker.on_orbita_event(event("message.sent", request_key="rk-9", dialog_id=777, direction="outgoing")))
    row = worker.db.deal(11)
    assert (row["status"], row["dialog_id"]) == ("sent_first", "777")
    assert row["followup_due_at"], "напоминание запланировано"
    assert bitrix.noted(11)


def test_message_failed_needs_check(worker, bitrix):
    worker.db.add_deal(12)
    worker.db.update(12, status="pending_first", request_key="rk-10", channel="max", phone="+9")
    run(worker.on_orbita_event(event("message.failed", request_key="rk-10", reason_code="send_failed")))
    assert worker.db.deal(12)["status"] == "needs_check"
    assert bitrix.noted(12)


def test_test_event_changes_nothing(worker):
    worker.db.add_deal(10)
    worker.db.update(10, status="sent_first", dialog_id="501")
    run(worker.on_orbita_event({"type": "message.received", "is_test": True,
                                "data": {"dialog_id": 501, "direction": "incoming"}}))
    assert worker.db.deal(10)["status"] == "sent_first"


# ---------- окно отправки ----------

class _FixedNow(datetime):
    moment = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.moment.astimezone(tz)


@pytest.mark.parametrize("utc_moment, inside", [
    ("2026-10-21T05:59:59+00:00", False),   # 08:59:59 по Москве
    ("2026-10-21T06:00:00+00:00", True),    # «С 09:00» — включительно
    ("2026-10-21T17:59:59+00:00", True),
    ("2026-10-21T18:00:00+00:00", False),   # «До 21:00» — в 21:00 уже нельзя
    ("2026-10-21T22:00:00+00:00", False),   # 01:00 по Москве, хотя в UTC ещё вечер
])
def test_in_window_bounds_in_robot_timezone(worker, monkeypatch, utc_moment, inside):
    worker.s.window_start, worker.s.window_end, worker.s.tz_name = "09:00", "21:00", "Europe/Moscow"
    monkeypatch.setattr(_FixedNow, "moment", datetime.fromisoformat(utc_moment))
    monkeypatch.setattr(worker_mod, "datetime", _FixedNow)
    assert worker.in_window() is inside


# ---------- шаблоны ----------

def test_render_substitutes_name_and_title():
    assert render("  {name}, заявка «{deal_title}»  ", "Анна", "Окна") == "Анна, заявка «Окна»"


@pytest.mark.parametrize("tpl, error", [
    ("{name} {oops}", KeyError),
    ("{0}", IndexError),
    ("{name", ValueError),
])
def test_render_template_errors(tpl, error):
    with pytest.raises(error):
        render(tpl, "Анна")


def test_no_name_uses_fallback_greeting(worker):
    worker.s.tpl_first, worker.s.tpl_no_name = "{name}!", "Здравствуйте"
    assert worker.render("first", "") == "Здравствуйте!"


# ---------- ручные действия во время шага робота ----------

def test_stop_while_first_message_in_progress_wins(running, bitrix, orbita):
    """«Остановить», нажатое, пока шаг ждёт Битрикс, не затирается результатом шага."""
    async def scenario():
        running.lock = WatchedLock()
        gate = Gate(bitrix, "get_contact")
        bitrix.new_ids.append(130)
        async with async_panel(running) as panel:
            tick = asyncio.create_task(running.tick())
            await gate.entered.wait()
            stop = asyncio.create_task(panel.post("/deal/130/stop"))
            await first_of(stop, running.lock.contended.wait())
            gate.opened.set()
            await asyncio.gather(tick, stop)
    run(scenario())
    assert running.db.deal(130)["status"] == "skipped"
    run(running.tick())
    assert running.db.deal(130)["status"] == "skipped" and orbita.replies == []


def test_client_replied_while_followup_in_progress_wins(running, orbita):
    running.db.add_deal(131)
    running.db.update(131, status="sent_first", dialog_id="D1", first_sent_at=ago(hours=25).isoformat(),
                      followup_due_at=ago(minutes=1).isoformat())
    orbita.history_items["D1"] = [out(ago(hours=25))]

    async def scenario():
        running.lock = WatchedLock()
        gate = Gate(orbita, "history")
        async with async_panel(running) as panel:
            tick = asyncio.create_task(running.tick())
            await gate.entered.wait()
            replied = asyncio.create_task(panel.post("/deal/131/replied"))
            await first_of(replied, running.lock.contended.wait())
            gate.opened.set()
            await asyncio.gather(tick, replied)
    run(scenario())
    assert running.db.deal(131)["status"] == "replied"


def test_disable_during_pass_stops_remaining_deals(running, bitrix, orbita):
    async def scenario():
        running.lock = WatchedLock()
        gate = Gate(bitrix, "get_contact")
        bitrix.new_ids += [132, 133]
        async with async_panel(running) as panel:
            tick = asyncio.create_task(running.tick())
            await gate.entered.wait()
            off = asyncio.create_task(panel.post("/settings/disable"))
            await first_of(off, running.lock.contended.wait())
            gate.opened.set()
            await asyncio.gather(tick, off)
    run(scenario())
    assert not running.s.enabled
    assert running.db.deal(133)["status"] == "new"
    assert [k for _, _, k in orbita.first] == [f"deal-132-first-{PHONE}"]
