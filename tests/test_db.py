"""Схема и миграции SQLite (db.py): установка старой версии обновляется при запуске, данные целы."""

import sqlite3

from first_contact import config
from first_contact.db import DB, service_lock


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
