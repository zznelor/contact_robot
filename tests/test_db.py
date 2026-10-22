"""Схема и миграции SQLite (db.py): установка старой версии обновляется при запуске, данные целы."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from conftest import PHONE, midday_tz, run
from first_contact import config
from first_contact.db import DB, service_lock
from first_contact.orbita import Message
from first_contact.worker import Worker


def test_old_orbita_events_are_done_after_upgrade():
    """В старой таблице событий Орбиты — только номера уже обработанных событий, без тел."""
    path = config.env().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE orbita_events (id TEXT PRIMARY KEY, type TEXT, at TEXT NOT NULL)")
    old.execute("INSERT INTO orbita_events VALUES ('7', 'message.received', '2026-10-20T10:00:00+00:00')")
    old.commit()
    old.close()

    db = DB(path)
    assert db.pending_events() == []
    assert not db.accept_event("7", "message.received", "{}", None), "повтор старого события — дубль"


def test_service_lock_is_exclusive_until_released():
    """Замок базы между процессами (реализация текущей ОС): второй захват не удаётся, пока первый держат;
    закрытие отпускает."""
    path = config.env().db_path
    held = service_lock(path)
    assert held is not None
    try:
        assert service_lock(path) is None
    finally:
        held.close()
    again = service_lock(path)
    assert again is not None
    again.close()


# Таблица сделок установки версии 1b52a60 (до намерений, pending_at и ключей шагов): исходная схема + её миграции
OLD_DEALS = """
CREATE TABLE deals (
    deal_id INTEGER PRIMARY KEY, status TEXT NOT NULL, name TEXT, phone TEXT, dialog_id TEXT, channel TEXT,
    request_key TEXT, first_text TEXT, first_sent_at TEXT, followup_due_at TEXT, followup_sent_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    client_replied_at TEXT, reply_due_at TEXT, reply_sent_at TEXT, checked_at TEXT,
    retry_n INTEGER NOT NULL DEFAULT 0, retried_at TEXT
)"""


def old_install(**deal) -> None:
    """База установки 1b52a60 с одной сделкой."""
    path = config.env().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(path)
    old.execute(OLD_DEALS)
    old.execute(f"INSERT INTO deals ({', '.join(deal)}) VALUES ({', '.join('?' * len(deal))})", tuple(deal.values()))
    old.commit()
    old.close()


def robot_on(bitrix, orbita) -> Worker:
    db = DB(config.env().db_path)
    s = config.load(db)
    s.bitrix_webhook, s.no_messenger_stage, s.orbita_api_key = "https://b.bitrix24.ru/rest/1/x/", "NO_MESSENGER", "k"
    s.window_start, s.window_end, s.tz_name = "06:00", "18:00", midday_tz()
    w = Worker(config.env(), s, db, bitrix, orbita)
    run(w.enable())
    return w


def test_old_unconfirmed_first_message_accepts_late_confirmation(bitrix, orbita):
    """Старая установка: первое сообщение передано (202) 40 минут назад, вебхука нет. После обновления робот
    зовёт человека, а пришедший позже message.sent этой отправки возвращает сделку в работу."""
    sent = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat(timespec="seconds")
    old_install(deal_id=300, status="pending_first", phone=PHONE, channel="whatsapp", request_key="rk-300",
                created_at=sent, updated_at=sent)
    w = robot_on(bitrix, orbita)
    run(w.tick())
    assert w.db.deal(300)["status"] == "needs_check"
    run(w.on_orbita_event({"type": "message.sent", "data": {"request_key": "rk-300", "dialog_id": 5}}))
    row = w.db.deal(300)
    assert row["status"] == "sent_first" and row["first_sent_at"] == sent


def test_step_key_saved_without_text_is_repeated(bitrix, orbita):
    """База версии, где у намерения напоминания был только ключ (без текста и без признака ручной остановки):
    исход прошлой отправки неизвестен, «Повторить» идёт тем же ключом; текст этой отправки запоминается для
    следующих повторов."""
    db = DB(config.env().db_path)
    for col in ("followup_text", "reply_text", "manual_stop_at"):
        db.conn.execute(f"ALTER TABLE deals DROP COLUMN {col}")
    db.add_deal(301)
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    db.update(301, status="error", dialog_id="D1", first_sent_at=day_ago, followup_due_at=day_ago,
              followup_key="deal-301-followup")
    db.conn.close()

    orbita.history_items["D1"] = [Message("", "", datetime.fromisoformat(day_ago), True)]   # клиент молчит
    w = robot_on(bitrix, orbita)
    assert run(w.retry(301, "a@x.ru")) == "sent_first"
    run(w.tick())
    (_, text, key), = orbita.replies
    assert key == "deal-301-followup"
    assert w.db.deal(301)["followup_text"] == text


def test_old_unconfirmed_first_message_in_needs_check_accepts_late_confirmation(bitrix, orbita):
    """Установка 1b52a60: таймаут подтверждения первого сообщения уже поставил «проверить вручную» (момента передачи
    та версия не хранила). После обновления исход этой отправки находит её по request_key; таблица исходов
    отправок появляется сама."""
    sent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    old_install(deal_id=302, status="needs_check", phone=PHONE, channel="whatsapp", request_key="rk-302",
                last_error="нет подтверждения отправки от Орбиты", created_at=sent, updated_at=sent)
    w = robot_on(bitrix, orbita)
    run(w.on_orbita_event({"type": "message.sent", "data": {"request_key": "rk-302", "dialog_id": 6,
                                                            "sent_at": sent}}))
    row = w.db.deal(302)
    assert (row["status"], row["dialog_id"], row["first_sent_at"]) == ("sent_first", "6", sent)


def test_old_install_reminder_key_reused_retries_with_new_key(bitrix, orbita):
    """Установка 1b52a60, клиент молчит сутки. Орбита отвергла ключ напоминания (idempotency_key_reused): после
    обновления «Повторить» идёт новым ключом — колонки, которыми это помечается, появляются сами."""
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    old_install(deal_id=303, status="sent_first", dialog_id="D1", phone=PHONE, first_sent_at=day_ago,
                followup_due_at=day_ago, created_at=day_ago, updated_at=day_ago)
    orbita.history_items["D1"] = [Message("", "", datetime.fromisoformat(day_ago), True)]
    orbita.reply_mode = "reused"
    w = robot_on(bitrix, orbita)
    run(w.tick())
    assert w.db.deal(303)["status"] == "needs_check"
    orbita.reply_mode = "sent"
    run(w.retry(303, "a@x.ru"))
    run(w.tick())
    assert [k for _, _, k in orbita.replies] == ["deal-303-followup", "deal-303-followup-r1"]


# ---------- поиск сделок ----------

@pytest.mark.parametrize("status, q, ids, total", [
    ("sent_first", "", [3, 2], 2),
    ("sent_first", "анна", [2], 1),
    ("", "анна", [2, 1], 2),
])
def test_find_by_status_and_text(status, q, ids, total):
    """Поиск сделок по статусу, по строке (ID, имя, телефон) и по обоим сразу: (первые, всего), новые первыми."""
    db = DB(config.env().db_path)
    for deal_id, st, name in ((1, "new", "Анна"), (2, "sent_first", "Анна"), (3, "sent_first", "Борис")):
        db.add_deal(deal_id)
        db.update(deal_id, status=st, name=name)
    rows, found = db.find(status, q)
    assert ([r["deal_id"] for r in rows], found) == (ids, total)


# ---------- ответ клиента по принятым вебхукам ----------

def received(direction: str, dialog_id: int = 501) -> str:
    """Тело message.received (спецификация: data — Message, обязательны id, dialog_id, channel, direction)."""
    return json.dumps({"type": "message.received", "data": {"id": 1, "dialog_id": dialog_id, "channel": "whatsapp",
                                                            "direction": direction}})


@pytest.mark.parametrize("bodies, expected", [
    ([received("incoming")], True),
    ([received("outgoing")], False),                    # direction — enum incoming | outgoing: наше — не ответ
    (["не JSON", received("incoming")], True),          # испорченное тело не скрывает следующее событие
])
def test_incoming_since_counts_only_client_messages(bodies, expected):
    db = DB(config.env().db_path)
    for n, body in enumerate(bodies):
        db.accept_event(f"ev-{n}", "message.received", body, "501")
    assert db.incoming_since("501", "2000-01-01T00:00:00+00:00") is expected
