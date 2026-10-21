"""Исход отправки (вебхуки message.sent / message.failed) сопоставляется с отправкой по request_key, а не по
статусу сделки. Эталон — docs/РАЗРАБОТЧИКУ.md, «Исход отправки по request_key»."""

from datetime import datetime

import pytest

from conftest import PHONE, UTC, ago, run
from first_contact import config
from first_contact.bitrix import BitrixError
from first_contact.worker import RETRYABLE, Worker
from test_worker import client_writes, due_followup, event, inc, out, passed_without_confirmation

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
    """Шаг падает до отправки (Битрикс или история Орбиты недоступны)."""
    async def down(*a):
        raise BitrixError("HTTP 503")
    if step == "reply":
        orbita.history = down
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
            vars(orbita).pop("history", None)
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
