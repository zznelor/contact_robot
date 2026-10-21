"""Логика робота (worker.py) на подделках Битрикса и Орбиты. Эталон — docs/РАЗРАБОТЧИКУ.md."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import (PHONE, UTC, Gate, WatchedLock, ago, async_panel, first_of, run, start_deal,
                      status_write_fails)
from first_contact import config
from first_contact import worker as worker_mod
from first_contact.bitrix import BitrixError
from first_contact.orbita import Message
from first_contact.worker import Worker, render


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


def test_late_bitrix_event_for_deal_before_enable_ignored(running, orbita):
    """Робот включён при последней сделке 100: запоздавшее событие о сделке 90 её не берёт."""
    run(running.on_deal_added(90))
    assert running.db.deal(90) is None and orbita.keys == []


def test_boundary_survives_polling_cursor(running, bitrix, orbita):
    """Граница включения не растёт вместе с курсором опроса: сделка 105 после опроса 110 — не «старая»."""
    start_deal(running, bitrix, 110)
    run(running.on_deal_added(105))
    assert running.db.deal(105)["status"] == "sent_first"


def test_take_into_work_allowed_for_deal_before_enable(running, bitrix, orbita):
    bitrix.created = ago(days=5)
    run(running.take(90, "a@x.ru"))
    assert running.db.deal(90)["status"] == "sent_first"


@pytest.mark.parametrize("created_hours_ago, expected", [(49, "skipped"), (47, "sent_first"), (None, "sent_first")])
def test_age_counted_from_deal_creation_in_bitrix(running, bitrix, created_hours_ago, expected):
    """48 ч — от DATE_CREATE сделки, а не от момента, когда робот её увидел (робот стоял, опрос отстал)."""
    bitrix.created = ago(hours=created_hours_ago) if created_hours_ago is not None else None
    assert start_deal(running, bitrix, 150)["status"] == expected


# ---------- первое сообщение ----------

def test_first_message_substitutes_name_and_title(running, bitrix, orbita):
    running.s.tpl_first = "{name} / {deal_title}"
    row = start_deal(running, bitrix, 102)
    assert row["status"] == "sent_first"
    assert orbita.first == [(PHONE, "Анна / Заявка", f"deal-102-first-{PHONE}-whatsapp")]


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

def test_failures_count_and_reset_when_deal_moves(running, bitrix, orbita, clock):
    orbita.mode = "boom"
    bitrix.new_ids = [122]
    for _ in range(4):
        run(running.tick())
        clock.advance(hours=1)
    row = running.db.deal(122)
    assert (row["status"], row["attempts"]) == ("new", 4)

    orbita.mode = "sent"
    run(running.tick())
    row = running.db.deal(122)
    assert (row["status"], row["attempts"]) == ("sent_first", 0)


def test_five_failures_in_a_row_is_error(running, bitrix, orbita, clock):
    orbita.mode = "boom"
    bitrix.new_ids = [123]
    for _ in range(5):
        run(running.tick())
        clock.advance(hours=1)
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
    assert [k for k in orbita.keys if k.endswith("-whatsapp")] == [f"deal-120-first-{PHONE}-whatsapp",
                                                                f"deal-120-first-{PHONE}-r1-whatsapp"]
    assert row["created_at"] == created_at and row["retried_at"]


@pytest.mark.parametrize("answer, expected", [("sent", "sent_first"), ("pending", "pending_first")])
def test_lost_result_write_keeps_intent_retry_goes_to_same_channel(running, bitrix, orbita, clock, answer, expected):
    """Орбита приняла первое сообщение в WhatsApp, а запись исхода в базу не удалась (процесс упал посреди
    перехода). К повтору порядок каналов сменили — повтор идёт тем же запросом, второе первое в Max не уходит."""
    orbita.answers["whatsapp"] = answer
    bitrix.new_ids.append(195)
    with status_write_fails(running.db, "NEW.status IN ('sent_first', 'pending_first')"):
        run(running.tick())
    assert running.db.deal(195)["status"] == "new"

    running.s.orbita_channels = ["max", "whatsapp"]
    restarted = Worker(config.env(), running.s, running.db, bitrix, orbita)
    clock.advance(minutes=3)
    run(restarted.tick())
    assert orbita.keys == [f"deal-195-first-{PHONE}-whatsapp"] * 2
    assert restarted.db.deal(195)["status"] == expected


def no_messenger_then_retry(w, bitrix, orbita, deal_id, pending=False):
    """Мессенджер не нашёлся (этап сменён), человек поправил телефон и нажал «Повторить»."""
    orbita.mode = "none"
    start_deal(w, bitrix, deal_id)
    assert bitrix.stage == "NO_MESSENGER"
    orbita.mode = "sent"
    if pending:
        orbita.answers["whatsapp"] = "pending"
    run(w.retry(deal_id, "a@x.ru"))
    run(w.on_deal_added(deal_id))


def test_successful_retry_returns_deal_to_previous_stage(running, bitrix, orbita):
    bitrix.stage = "IN_WORK"
    no_messenger_then_retry(running, bitrix, orbita, 190)
    assert running.db.deal(190)["status"] == "sent_first"
    assert bitrix.stages_set == [(190, "NO_MESSENGER"), (190, "IN_WORK")]
    assert len([n for d, n in bitrix.notes if d == 190]) >= 3, "о возврате этапа — комментарий в сделке"


def test_retry_confirmed_by_webhook_returns_previous_stage(running, bitrix, orbita):
    bitrix.stage = "IN_WORK"
    no_messenger_then_retry(running, bitrix, orbita, 191, pending=True)
    row = running.db.deal(191)
    assert row["status"] == "pending_first" and bitrix.stages_set == [(191, "NO_MESSENGER")]
    run(running.on_orbita_event(event("message.sent", request_key=row["request_key"], dialog_id=5)))
    assert bitrix.stages_set == [(191, "NO_MESSENGER"), (191, "IN_WORK")]


def test_stage_return_retried_after_bitrix_error(running, bitrix, orbita, clock):
    """Первое сообщение после «Повторить» ушло, а Битрикс не дал вернуть этап — возврат повторяется сам."""
    bitrix.stage = "IN_WORK"
    real, failed = bitrix.set_stage, []

    async def flaky(deal_id, stage_id):
        if stage_id == "IN_WORK" and not failed:
            failed.append(stage_id)
            raise BitrixError("crm.deal.update: HTTP 503")
        await real(deal_id, stage_id)
    bitrix.set_stage = flaky
    no_messenger_then_retry(running, bitrix, orbita, 196)
    assert running.db.deal(196)["status"] == "sent_first" and bitrix.stage == "NO_MESSENGER"

    clock.advance(minutes=3)
    run(running.tick())
    assert bitrix.stages_set == [(196, "NO_MESSENGER"), (196, "IN_WORK")]
    assert len(orbita.first) == 1, "первое сообщение заново не отправляется"


def test_stage_returned_after_restart_while_confirming(running, bitrix, orbita):
    """message.sent по «Повторить» записан, а процесс остановили, пока шёл комментарий в Битрикс."""
    bitrix.stage = "IN_WORK"
    no_messenger_then_retry(running, bitrix, orbita, 197, pending=True)
    rk = running.db.deal(197)["request_key"]
    running.db.accept_event("e197", "message.sent", json.dumps(event("message.sent", request_key=rk, dialog_id=5)),
                            "5")

    async def stopped(*a):
        raise asyncio.CancelledError
    bitrix.comment = stopped
    with pytest.raises(asyncio.CancelledError):
        run(running.process_inbox())
    del bitrix.comment

    run(Worker(config.env(), running.s, running.db, bitrix, orbita).tick())
    assert bitrix.stages_set[-1] == (197, "IN_WORK")


def test_previous_stage_remembered_when_status_write_lost(running, bitrix, orbita, clock):
    """Сделка уже перенесена на «Не нашёл мессенджер», а запись статуса не удалась (обрыв посреди перехода)."""
    bitrix.stage, orbita.mode = "IN_WORK", "none"
    bitrix.new_ids.append(198)
    with status_write_fails(running.db, "NEW.status = 'no_messenger'"):
        run(running.tick())
    assert bitrix.stage == "NO_MESSENGER"
    clock.advance(minutes=3)
    run(running.tick())
    assert running.db.deal(198)["status"] == "no_messenger"

    orbita.mode = "sent"
    run(running.retry(198, "a@x.ru"))
    run(running.on_deal_added(198))
    assert bitrix.stages_set[-1] == (198, "IN_WORK")


def test_retry_does_not_move_deal_the_manager_already_moved(running, bitrix, orbita):
    bitrix.stage = "IN_WORK"
    orbita.mode = "none"
    start_deal(running, bitrix, 192)
    bitrix.stage = "CALL_BACK"                          # менеджер сам увёл сделку с «Не нашёл мессенджер»
    orbita.mode = "sent"
    run(running.retry(192, "a@x.ru"))
    run(running.on_deal_added(192))
    assert running.db.deal(192)["status"] == "sent_first"
    assert bitrix.stages_set == [(192, "NO_MESSENGER")]


def test_retry_of_deal_moved_before_update_leaves_stage(running, bitrix, orbita):
    """Прежний этап сделки, перенесённой версией без этой функции, неизвестен — этап не трогаем."""
    running.db.add_deal(193)
    running.db.update(193, status="no_messenger", last_error="мессенджер не найден")
    bitrix.stage = "NO_MESSENGER"
    run(running.retry(193, "a@x.ru"))
    run(running.on_deal_added(193))
    assert running.db.deal(193)["status"] == "sent_first" and bitrix.stages_set == []


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
    assert [k for _, _, k in orbita.replies] == ["deal-120-followup-r1"], "повтор шага — новый ключ"


def test_bitrix_down_does_not_stop_other_steps(replying, bitrix, orbita):
    """Опрос Битрикса падает — ответ по шаблону и таймаут неподтверждённой отправки всё равно идут."""
    client_writes(replying, bitrix, orbita, 170, lambda first: [out(first), inc(datetime.now(UTC))])
    replying.db.add_deal(171)
    replying.db.update(171, status="pending_first", request_key="rk-171")
    replying.db.conn.execute("UPDATE deals SET updated_at=? WHERE deal_id=171", (ago(hours=1).isoformat(),))

    async def down(*a):
        raise RuntimeError("Битрикс недоступен")
    bitrix.deal_ids_after = down
    run(replying.tick())
    assert [k for _, _, k in orbita.replies] == ["deal-170-reply"]
    assert replying.db.deal(171)["status"] == "needs_check"


def test_failed_attempt_waits_before_next(running, bitrix, orbita, clock):
    """После сбоя следующая попытка — не на ближайшем проходе, а после паузы."""
    orbita.mode = "boom"
    start_deal(running, bitrix, 172)
    run(running.tick())
    assert len(orbita.keys) == 1 and running.db.deal(172)["attempts"] == 1
    clock.advance(minutes=3)
    run(running.tick())
    assert len(orbita.keys) == 2, "пауза прошла — новая попытка"


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


def test_followup_with_incoming_without_time_needs_check(running, orbita):
    """Входящее без времени: не «клиент ответил» (ответ по шаблону его бы не нашёл) и не «молчит»."""
    due_followup(running, orbita, 125)
    orbita.history_items["D1"] = [out(ago(hours=25)), inc(None)]
    run(running.tick())
    assert running.db.deal(125)["status"] == "needs_check"
    assert orbita.replies == []


@pytest.mark.parametrize("history, expected", [
    ([out(ago(days=4))], "no_reply"),                     # молчит — напоминание через трое суток не шлём
    ([out(ago(days=4)), inc(ago(days=3))], "replied"),    # ответил — фиксируем ответ
])
def test_followup_overdue_more_than_48h_not_sent(running, orbita, history, expected):
    """Робот или Орбита стояли: срок напоминания прошёл больше 48 ч назад."""
    running.db.add_deal(126)
    running.db.update(126, status="sent_first", dialog_id="D1", first_sent_at=ago(days=4).isoformat(),
                      followup_due_at=ago(days=3).isoformat())
    orbita.history_items["D1"] = history
    run(running.tick())
    assert running.db.deal(126)["status"] == expected
    assert orbita.replies == []


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


def test_long_reply_pause_still_answers(replying, orbita):
    """Пауза ответа 30 мин (форма разрешает до 60): проход опоздал на 40 с — ответ всё равно уходит."""
    replying.s.reply_delay_minutes = 30
    replying.db.add_deal(127)
    replying.db.update(127, status="replied", dialog_id="D1", first_sent_at=ago(minutes=40).isoformat(),
                       client_replied_at=ago(minutes=30, seconds=40).isoformat(),
                       reply_due_at=ago(seconds=40).isoformat())
    orbita.history_items["D1"] = [out(ago(minutes=40)), inc(ago(minutes=30, seconds=40))]
    run(replying.tick())
    assert [k for _, _, k in orbita.replies] == ["deal-127-reply"]


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


def test_late_message_sent_after_timeout_confirms_first(running, bitrix, orbita, clock):
    """Орбита подтвердила первое сообщение позже 30 минут: сделка уже «проверить вручную» — подтверждение
    по сохранённому request_key возвращает её в работу, «Повторить» второе первое не выпустит."""
    orbita.answers["whatsapp"] = "pending"
    rk = start_deal(running, bitrix, 187)["request_key"]
    clock.advance(minutes=31)
    run(running.tick())
    assert running.db.deal(187)["status"] == "needs_check"

    run(running.on_orbita_event(event("message.sent", request_key=rk, dialog_id=778)))
    row = running.db.deal(187)
    assert (row["status"], row["dialog_id"]) == ("sent_first", "778") and row["first_sent_at"]


def test_message_failed_needs_check(worker, bitrix):
    worker.db.add_deal(12)
    worker.db.update(12, status="pending_first", request_key="rk-10", channel="max", phone="+9")
    run(worker.on_orbita_event(event("message.failed", request_key="rk-10", reason_code="send_failed")))
    assert worker.db.deal(12)["status"] == "needs_check"
    assert bitrix.noted(12)


def due_followup(w, orbita, deal_id):
    """Первое ушло сутки назад, клиент молчит — пора напоминание."""
    w.db.add_deal(deal_id)
    w.db.update(deal_id, status="sent_first", dialog_id="D1", first_sent_at=ago(hours=25).isoformat(),
                followup_due_at=ago(minutes=1).isoformat())
    orbita.history_items["D1"] = [out(ago(hours=25))]


def test_followup_202_then_failed_needs_check(running, bitrix, orbita):
    orbita.reply_mode = "pending"
    due_followup(running, orbita, 160)
    run(running.tick())
    (_, _, key), = orbita.replies
    run(running.on_orbita_event(event("message.failed", request_key=f"rk-{key}", reason_code="send_failed")))
    row = running.db.deal(160)
    assert row["status"] == "needs_check" and not row["followup_sent_at"]
    assert bitrix.noted(160)


def test_followup_202_then_sent_stays_sent_followup(running, orbita):
    orbita.reply_mode = "pending"
    due_followup(running, orbita, 161)
    run(running.tick())
    (_, _, key), = orbita.replies
    run(running.on_orbita_event(event("message.sent", request_key=f"rk-{key}", dialog_id="D1")))
    row = running.db.deal(161)
    assert row["status"] == "sent_followup" and row["followup_sent_at"]


def test_retry_after_failed_followup_resends_with_new_key(running, orbita):
    orbita.reply_mode = "pending"
    due_followup(running, orbita, 162)
    run(running.tick())
    run(running.on_orbita_event(event("message.failed", request_key="rk-deal-162-followup")))
    assert run(running.retry(162, "a@x.ru")) == "sent_first"
    run(running.tick())
    assert [k for _, _, k in orbita.replies] == ["deal-162-followup", "deal-162-followup-r1"]


def test_retry_sends_old_undelivered_template_reply(replying, orbita):
    """Ответ по шаблону не доставлен (message.failed), человек нажал «Повторить» через час после сообщения
    клиента — ручной повтор продлевает срок, ответ уходит с новым ключом."""
    replying.db.add_deal(166)
    replying.db.update(166, status="needs_check", dialog_id="D1", first_sent_at=ago(hours=2).isoformat(),
                       client_replied_at=ago(hours=1).isoformat(), reply_due_at=ago(minutes=50).isoformat(),
                       last_error="ответ по шаблону не доставлен(о): send_failed")
    orbita.history_items["D1"] = [out(ago(hours=2)), inc(ago(hours=1))]
    assert run(replying.retry(166, "a@x.ru")) == "replied"
    run(replying.tick())
    assert [k for _, _, k in orbita.replies] == ["deal-166-reply-r1"]


def test_retry_after_lost_followup_answer_repeats_same_key(running, orbita):
    """Ответ Орбиты на напоминание потерян (могло уйти), человек остановил и нажал «Повторить» — повтор
    тем же ключом: Орбита вернёт исход прошлого запроса, второе напоминание не уйдёт."""
    orbita.reply_mode = "lost"
    due_followup(running, orbita, 164)
    run(running.tick())
    run(running.stop(164, "a@x.ru"))
    orbita.reply_mode = "sent"
    assert run(running.retry(164, "a@x.ru")) == "sent_first"
    run(running.tick())
    assert [k for _, _, k in orbita.replies] == ["deal-164-followup"] * 2


def test_retry_after_lost_template_reply_answer_repeats_same_key(replying, bitrix, orbita):
    orbita.reply_mode = "lost"
    client_writes(replying, bitrix, orbita, 165, lambda first: [out(first), inc(datetime.now(UTC))])
    run(replying.tick())
    replying.db.update(165, status="error")             # как после пяти сбоев подряд
    orbita.reply_mode = "sent"
    assert run(replying.retry(165, "a@x.ru")) == "replied"
    run(replying.tick())
    assert [k for _, _, k in orbita.replies] == ["deal-165-reply"] * 2


def test_template_reply_202_then_failed_needs_check_and_retry_resends(replying, bitrix, orbita):
    orbita.reply_mode = "pending"
    client_writes(replying, bitrix, orbita, 163, lambda first: [out(first), inc(datetime.now(UTC))])
    run(replying.tick())
    run(replying.on_orbita_event(event("message.failed", request_key="rk-deal-163-reply")))
    row = replying.db.deal(163)
    assert row["status"] == "needs_check" and not row["reply_sent_at"]

    assert run(replying.retry(163, "a@x.ru")) == "replied"
    run(replying.tick())
    assert [k for _, _, k in orbita.replies] == ["deal-163-reply", "deal-163-reply-r1"]


def test_early_client_reply_survives_restart_while_confirming(replying, bitrix, orbita):
    """Клиент ответил до подтверждения первого сообщения; message.sent записан, а процесс остановили, пока шёл
    комментарий в Битрикс. После перезапуска ответ клиента не теряется."""
    orbita.answers["whatsapp"] = "pending"
    rk = start_deal(replying, bitrix, 186)["request_key"]
    db = replying.db
    db.accept_event("r186", "message.received",
                    json.dumps(event("message.received", dialog_id=777, direction="incoming")), "777")
    run(replying.process_inbox())
    db.accept_event("s186", "message.sent", json.dumps(event("message.sent", request_key=rk, dialog_id=777)), "777")

    async def stopped(*a):
        raise asyncio.CancelledError
    bitrix.comment = stopped
    with pytest.raises(asyncio.CancelledError):
        run(replying.process_inbox())
    del bitrix.comment

    run(Worker(config.env(), replying.s, db, bitrix, orbita).tick())
    row = db.deal(186)
    assert row["status"] == "replied" and row["client_replied_at"]


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
    assert [k for _, _, k in orbita.first] == [f"deal-132-first-{PHONE}-whatsapp"]
