"""Исход отправки (вебхуки message.sent / message.failed) сопоставляется с отправкой по request_key, а не по
статусу сделки. Эталон — docs/РАЗРАБОТЧИКУ.md, «Исход отправки по request_key»."""

from datetime import datetime

import pytest

from conftest import PHONE, UTC, ago, run
from first_contact import config
from first_contact.bitrix import BitrixError
from first_contact.orbita import OrbitaError
from first_contact.worker import RETRYABLE, Worker
from test_worker import (client_writes, due_followup, event, inc, journal, notes_of, out,
                         passed_without_confirmation)

D = 180
KEY = {"first": f"deal-{D}-first-{PHONE}-whatsapp", "followup": f"deal-{D}-followup", "reply": f"deal-{D}-reply"}
KEY_COL = {"first": "request_key", "followup": "followup_request_key", "reply": "reply_request_key"}
FACT = {"first": "first_sent_at", "followup": "followup_sent_at", "reply": "reply_sent_at"}


def sends(orbita, step) -> list[str]:
    """Ключи всех запросов шага в Орбиту."""
    if step == "first":
        return list(orbita.keys)
    return [k for _, _, k in orbita.replies if f"-{step}" in k]


def outcome(w, step, ok):
    kind = "message.sent" if ok else "message.failed"
    run(w.on_orbita_event(event(kind, request_key=f"rk-{KEY[step]}", dialog_id="D1", reason_code="send_failed")))


def handed_over(w, bitrix, orbita, step, answer="pending"):
    """Шаг передан в Орбиту: pending — ответ 202 (ждём вебхук), lost — Орбита приняла запрос, ответ потерян."""
    if step == "first":
        orbita.answers["whatsapp"] = answer
        bitrix.new_ids.append(D)
        run(w.tick())
        return
    orbita.reply_mode = answer
    if step == "followup":
        due_followup(w, orbita, D)
    else:
        w.s.reply_on, w.s.reply_delay_minutes = True, 0
        client_writes(w, bitrix, orbita, D, lambda first: [out(first), inc(datetime.now(UTC))])
    run(w.tick())


def accept_again(orbita, step):
    """Повтор того же запроса: Орбита отвечает 202 с тем же request_key."""
    if step == "first":
        orbita.answers["whatsapp"] = "pending"
    else:
        orbita.reply_mode = "pending"


def step_fails(w, bitrix, orbita, step):
    """Шаг падает: Битрикс недоступен до отправки; ответ по шаблону с незавершённой отправкой Битрикс и историю не
    читает (повтор идёт сразу) — у него Орбита недоступна на отправке, исход повтора неизвестен."""
    async def down(*a):
        raise BitrixError("HTTP 503")

    async def orbita_down(*a):
        raise OrbitaError("HTTP 503: Орбита недоступна")
    if step == "reply":
        orbita.send_reply = orbita_down
    else:
        bitrix.get_deal = down


def reach(w, bitrix, orbita, clock, step, path, ok):
    """Довести отправку шага до пути path и доставить её исход. Возвращает робота (после перезапуска — нового)."""
    if path == "before_key":
        # Орбита приняла запрос, ответ потерян; исход пришёл раньше, чем робот узнал request_key; повтор того же
        # запроса (после перезапуска) снова 202 с тем же ключом
        handed_over(w, bitrix, orbita, step, "lost")
        assert not w.db.deal(D)[KEY_COL[step]]
        outcome(w, step, ok)
        accept_again(orbita, step)
        w = Worker(config.env(), w.s, w.db, bitrix, orbita)
        clock.advance(minutes=3)
        run(w.tick())
        return w
    handed_over(w, bitrix, orbita, step)
    assert w.db.deal(D)[KEY_COL[step]]
    if path == "stop":
        run(w.stop(D, "a@x.ru"))
    elif path == "mark_replied":
        run(w.mark_replied(D, "a@x.ru"))
    elif path in ("needs_check", "error", "retry"):
        clock.advance(minutes=31)
        run(w.tick())
        assert w.db.deal(D)["status"] == "needs_check"
        if path in ("error", "retry"):
            run(w.retry(D, "a@x.ru"))
        if path == "error":
            step_fails(w, bitrix, orbita, step)
            for _ in range(5):
                run(w.tick())
                clock.advance(minutes=20)
            assert w.db.deal(D)["status"] == "error"
            vars(bitrix).pop("get_deal", None)
            vars(orbita).pop("send_reply", None)
    outcome(w, step, ok)
    return w


PATHS = ["before_key", "after_key", "stop", "mark_replied", "needs_check", "error", "retry"]

# Статус после подтверждённой доставки: из ожидающих — «шаг сделан»; ручное решение не меняется
DELIVERED = {
    "first": {"stop": "skipped", "mark_replied": "replied", None: "sent_first"},
    "followup": {"stop": "skipped", "mark_replied": "replied", None: "sent_followup"},
    "reply": {None: "replied"},
}
# Статус после доказанного отказа
FAILED = {
    "first": {"stop": "skipped", "mark_replied": "replied", "error": "error", "retry": "new", None: "needs_check"},
    "followup": {"stop": "skipped", "mark_replied": "replied", "error": "error", "retry": "sent_first",
                 None: "needs_check"},
    "reply": {"stop": "replied", "mark_replied": "replied", "error": "error", "retry": "replied", None: "needs_check"},
}


@pytest.mark.parametrize("ok", [True, False], ids=["sent", "failed"])
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("step", ["first", "followup", "reply"])
def test_outcome_applied_by_request_key_on_every_path(running, bitrix, orbita, clock, step, path, ok):
    """Исход отправки записывается в поля доставки по её request_key, в каком бы статусе ни была сделка; статус
    меняется только из ожидающих состояний и не при ручном решении. После подтверждённой доставки «Повторить»
    этот шаг не повторяет; после доказанного отказа — повторяет новым ключом."""
    w = reach(running, bitrix, orbita, clock, step, path, ok)
    row = w.db.deal(D)
    assert bool(row[FACT[step]]) is ok, "факт доставки — по исходу, независимо от статуса"
    table = DELIVERED if ok else FAILED
    assert row["status"] == table[step].get(path, table[step][None])
    assert not row[KEY_COL[step]], "исход получен — отправка больше ничего не ждёт"
    if row["status"] == "sent_followup":
        assert row["followup_sent_at"]

    before = sends(orbita, step)
    orbita.answers.pop("whatsapp", None)
    orbita.reply_mode = "sent"
    if row["status"] in RETRYABLE:
        run(w.retry(D, "a@x.ru"))
    clock.advance(minutes=3)
    run(w.tick())
    again = sends(orbita, step)[len(before):]
    # Шаг продолжается после отказа, если человек не решил иначе: «Клиент ответил» снимает и первое, и напоминание,
    # остановленный ответ по шаблону не планируется заново
    resend = not ok and path != "mark_replied" and not (step == "reply" and path == "stop")
    if resend:
        assert again and again[0] != KEY[step], "после отказа — новая отправка с новым ключом"
    else:
        assert again == [], "доставленный шаг не повторяется"


def test_late_reminder_confirmation_after_unreadable_history_counts(running, bitrix, orbita, clock):
    """«Повторить» после таймаута подтверждения напоминания, а повтор не ушёл: история не читается — «проверить
    вручную». Поздний message.sent прежней отправки засчитывает напоминание, итог выносится в свой срок."""
    passed_without_confirmation(running, bitrix, orbita, "followup", D)
    clock.advance(minutes=31)
    run(running.tick())
    run(running.retry(D, "a@x.ru"))
    orbita.history_items["D1"] = [out(ago(hours=25)), inc(None)]      # входящее без времени — «не понять»
    run(running.tick())
    assert running.db.deal(D)["status"] == "needs_check"

    outcome(running, "followup", True)
    row = running.db.deal(D)
    assert row["status"] == "sent_followup" and row["followup_sent_at"]
    orbita.history_items["D1"] = [out(ago(hours=25))]
    clock.advance(hours=running.s.followup_hours + 1)
    run(running.tick())
    assert running.db.deal(D)["status"] == "no_reply"
    assert len(sends(orbita, "followup")) == 1


def test_outcomes_older_than_30_days_removed(worker):
    """Исход, так и не нашедший отправку (например, тестового сообщения из настроек), не копится вечно."""
    db = worker.db
    db.save_outcome("rk-old", True, None, "D1", None)
    db.save_outcome("rk-new", True, None, "D1", None)
    db.conn.execute("UPDATE send_outcomes SET at=? WHERE request_key='rk-old'", (ago(days=31).isoformat(timespec="seconds"),))
    run(worker.tick())
    assert (db.outcome("rk-old"), bool(db.outcome("rk-new"))) == (None, True)


def lost_first_answer(w, bitrix, orbita, ok):
    """Первое сообщение: Орбита приняла запрос, ответ до робота не дошёл (в сделке — намерение, request_key пуст);
    исход пришёл раньше, чем робот узнал request_key. Повтор того же запроса снова получит 202 с тем же ключом."""
    handed_over(w, bitrix, orbita, "first", "lost")
    row = w.db.deal(D)
    assert row["intent_key"] == KEY["first"] and not row["request_key"]
    outcome(w, "first", ok)
    accept_again(orbita, "first")


@pytest.mark.parametrize("ok", [True, False], ids=["sent", "failed"])
@pytest.mark.parametrize("change", ["contact_removed", "deal_closed", "other_funnel", "no_messenger_stage"])
def test_unfinished_first_send_restored_before_fresh_bitrix_data(running, bitrix, orbita, clock, change, ok):
    """Исход незавершённой отправки первого сообщения выясняется повтором того же запроса раньше, чем робот решает
    по свежим данным Битрикса: менеджер отвязал контакт, закрыл сделку, перенёс её в другую воронку или на этап
    «Не нашёл мессенджер» — сообщение, которое могло уйти, всё равно сопоставляется со своим исходом."""
    lost_first_answer(running, bitrix, orbita, ok)
    if change == "contact_removed":
        bitrix.phones = []
    elif change == "deal_closed":
        bitrix.semantic = "F"
    elif change == "other_funnel":
        bitrix.category = "5"
    else:
        bitrix.stage = "NO_MESSENGER"
    w = Worker(config.env(), running.s, running.db, bitrix, orbita)
    clock.advance(minutes=11)                           # и 10 минут ожидания телефона прошли
    run(w.tick())

    row = w.db.deal(D)
    assert w.db.outcome(f"rk-{KEY['first']}")["applied_at"], "исход сопоставлен с отправкой"
    assert bool(row["first_sent_at"]) is ok
    assert row["status"] == ("sent_first" if ok else "needs_check")
    assert not row["request_key"] and not row["intent_key"]
    assert orbita.keys == [KEY["first"], KEY["first"]], "повтор — тот же запрос, нового нет"
    assert bitrix.stages_set == [], "уже переданную заявку не уводят на «Не нашёл мессенджер»"


def test_unfinished_first_send_older_than_48h_goes_to_human(running, bitrix, orbita, clock):
    """Незавершённая отправка, повтор которой выпал бы за 48 ч: повтор мог бы стать первой отправкой спустя двое
    суток, поэтому его нет, — но и молча пропускать сделку нельзя: сообщение могло уйти. «Проверить вручную»."""
    handed_over(running, bitrix, orbita, "first", "lost")
    clock.advance(hours=49)
    run(running.tick())
    row = running.db.deal(D)
    assert row["status"] == "needs_check" and row["intent_key"] == KEY["first"]
    assert orbita.keys == [KEY["first"]]


@pytest.mark.parametrize("ok", [True, False], ids=["sent", "failed"])
def test_unfinished_template_reply_restored_although_own_message_in_history(running, bitrix, orbita, clock, ok):
    """Ответ по шаблону: Орбита приняла запрос, ответ потерян; исход пришёл раньше, чем робот узнал request_key, а
    в истории диалога после сообщения клиента уже есть исходящее — это сам ответ робота, а не менеджера. Повтор того
    же запроса выясняет request_key, и исход сопоставляется: свой ответ не отменяет его как «менеджер уже ответил»."""
    handed_over(running, bitrix, orbita, "reply", "lost")
    row = running.db.deal(D)
    assert row["reply_key"] == KEY["reply"] and not row["reply_request_key"]
    orbita.history_items["D1"].append(out(datetime.now(UTC)))
    outcome(running, "reply", ok)
    accept_again(orbita, "reply")
    w = Worker(config.env(), running.s, running.db, bitrix, orbita)
    clock.advance(minutes=3)
    run(w.tick())

    row = w.db.deal(D)
    assert w.db.outcome(f"rk-{KEY['reply']}")["applied_at"], "исход сопоставлен с отправкой"
    assert bool(row["reply_sent_at"]) is ok
    assert row["status"] == ("replied" if ok else "needs_check")
    assert not row["reply_request_key"]
    assert sends(orbita, "reply") == [KEY["reply"], KEY["reply"]], "повтор — тот же запрос"


@pytest.mark.parametrize("late, resent", [({"minutes": 40}, True), ({"hours": 49}, False)], ids=["40min", "49h"])
def test_unfinished_template_reply_late(running, bitrix, orbita, clock, late, resent):
    """Незавершённый ответ по шаблону, а робот вернулся к нему позже 30 минут от срока ответа: отсечка «опоздал»
    отменяет новую отправку, а не выяснение переданной — повтор тем же ключом. Позже 48 ч повтор мог бы стать первой
    отправкой спустя двое суток — его нет, но и молча отменять нельзя: ответ мог уйти. «Проверить вручную»."""
    handed_over(running, bitrix, orbita, "reply", "lost")
    accept_again(orbita, "reply")
    clock.advance(**late)
    run(running.tick())
    row = running.db.deal(D)
    if resent:
        assert sends(orbita, "reply") == [KEY["reply"], KEY["reply"]]
        assert row["status"] == "replied" and row["reply_request_key"] == f"rk-{KEY['reply']}"
    else:
        assert sends(orbita, "reply") == [KEY["reply"]]
        assert row["status"] == "needs_check" and row["reply_key"] == KEY["reply"]


# Журнал исхода: «мессенджер подтвердил доставку» (напоминание, ответ), причина отказа; при ручном решении —
# «статус … не меняется» (docs/РАЗРАБОТЧИКУ.md, «Исход отправки по request_key»)
JOURNAL_CASES = [
    # step, path, ok, запись с этим в названии события (None — не задана документом), «не меняется»
    ("first", "needs_check", True, None, False),
    ("first", "needs_check", False, None, False),
    ("first", "stop", True, "мессенджер подтвердил доставку", True),
    ("first", "stop", False, None, True),
    ("followup", "needs_check", True, "мессенджер подтвердил доставку", False),
    ("followup", "needs_check", False, None, False),
    ("followup", "stop", True, "мессенджер подтвердил доставку", True),
    ("followup", "stop", False, None, True),
    ("reply", "needs_check", True, "мессенджер подтвердил доставку", False),
    ("reply", "needs_check", False, None, False),
    ("reply", "stop", False, None, True),
]


@pytest.mark.parametrize("step, path, ok, named, unchanged", JOURNAL_CASES)
def test_outcome_recorded_in_deal_journal(running, bitrix, orbita, clock, step, path, ok, named, unchanged):
    w = reach(running, bitrix, orbita, clock, step, path, ok)
    entries = journal(w.db, D)
    if named:
        assert any(named in e for e, _ in entries)
    if not ok:
        assert any("send_failed" in e + d for e, d in entries), "причина отказа — в журнале сделки"
    assert any("не меняется" in d for _, d in entries) is unchanged


@pytest.mark.parametrize("step", ["first", "followup", "reply"])
def test_failure_reason_shown_in_card(running, bitrix, orbita, clock, step):
    w = reach(running, bitrix, orbita, clock, step, "needs_check", False)
    assert "send_failed" in w.db.deal(D)["last_error"]


@pytest.mark.parametrize("status", ["no_messenger", "skipped"])
def test_late_confirmation_does_not_undo_decision_of_new_attempt(worker, status):
    """«Не нашёл мессенджер» новой попытки или пропуск — решение, которое исход прежней отправки не отменяет;
    факт доставки записывается."""
    worker.db.add_deal(D)
    worker.db.update(D, status=status, request_key="rk-x", pending_at=ago(hours=1).isoformat(timespec="seconds"),
                     channel="whatsapp", phone=PHONE)
    run(worker.on_orbita_event(event("message.sent", request_key="rk-x", dialog_id="D1")))
    row = worker.db.deal(D)
    assert row["status"] == status and row["first_sent_at"] and not row["request_key"]


def test_repeated_confirmation_of_recorded_first_message_releases_key(worker):
    """Факт доставки уже записан, а ключ ждёт исхода (строка старой версии): исход применён ⇒ ключ снят."""
    worker.db.add_deal(D)
    worker.db.update(D, status="sent_first", first_sent_at=ago(hours=1).isoformat(timespec="seconds"),
                     dialog_id="D1", request_key="rk-old")
    run(worker.on_orbita_event(event("message.sent", request_key="rk-old", dialog_id="D1")))
    assert not worker.db.deal(D)["request_key"] and worker.db.outcome("rk-old")["applied_at"]


@pytest.mark.parametrize("pending_at", [None, ago(minutes=5).isoformat(timespec="seconds")],
                         ids=["old_version_row", "no_dialog_in_event"])
def test_confirmation_without_time_or_dialog_then_reminder(running, bitrix, orbita, clock, pending_at):
    """Подтверждение без времени отправки и без номера диалога (строка версии до pending_at; событие без
    dialog_id): первое засчитано, напоминание уходит в свой срок."""
    running.db.add_deal(D)
    running.db.update(D, status="needs_check", request_key="rk-old", channel="whatsapp", phone=PHONE,
                      pending_at=pending_at, dialog_id="D1" if pending_at else None)
    ev = event("message.sent", request_key="rk-old")
    if pending_at is None:
        ev["data"]["dialog_id"] = "D1"
    run(running.on_orbita_event(ev))
    assert running.db.deal(D)["status"] == "sent_first"
    orbita.history_items["D1"] = [out(datetime.now(UTC))]            # наше первое; клиент молчит
    clock.advance(hours=running.s.followup_hours, minutes=1)
    run(running.tick())
    assert running.db.deal(D)["status"] == "sent_followup"


def test_outcome_received_before_key_comments_first_message(running, bitrix, orbita, clock):
    """Исход первого сообщения пришёл раньше, чем робот узнал request_key: при записи ключа — комментарий в сделку."""
    lost_first_answer(running, bitrix, orbita, True)
    notes = len(notes_of(bitrix, D))
    w = Worker(config.env(), running.s, running.db, bitrix, orbita)
    clock.advance(minutes=3)
    run(w.tick())
    assert w.db.deal(D)["status"] == "sent_first" and len(notes_of(bitrix, D)) > notes


def test_failure_received_before_key_comments_template_reply(running, bitrix, orbita, clock):
    """Отказ ответа по шаблону пришёл раньше ключа: при записи ключа — комментарии «передал» и «не доставлен»."""
    handed_over(running, bitrix, orbita, "reply", "lost")
    outcome(running, "reply", False)
    accept_again(orbita, "reply")
    notes = len(notes_of(bitrix, D))
    w = Worker(config.env(), running.s, running.db, bitrix, orbita)
    clock.advance(minutes=3)
    run(w.tick())
    assert w.db.deal(D)["status"] == "needs_check" and len(notes_of(bitrix, D)) == notes + 2
