"""Схема и миграции SQLite (db.py): установка старой версии обновляется при запуске, данные целы."""

import sqlite3
from datetime import datetime, timedelta, timezone

from conftest import PHONE, midday_tz, run
from first_contact import config
from first_contact.db import DB, service_lock
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
