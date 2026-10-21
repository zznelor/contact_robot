"""Командная строка (__main__.py) отдельным процессом: разовые команды и запущенный сервис."""

import socket
import subprocess
import sys

import pytest

from conftest import midday_tz
from first_contact import config
from first_contact.db import DB, service_lock


def cli(*args: str) -> subprocess.CompletedProcess:
    """python -m first_contact … с окружением теста (DB_PATH — база этого теста)."""
    return subprocess.run([sys.executable, "-m", "first_contact", *args], capture_output=True, text=True,
                          timeout=30)


@pytest.mark.parametrize("args", [("once",), ("process", "5"), ("run", "0")])
def test_robot_commands_refuse_while_service_holds_database(args):
    """Сервис держит базу: второй процесс робота на ней не работает — ни разовый проход, ни второй сервис."""
    path = config.env().db_path
    DB(path).conn.close()
    held = service_lock(path)
    assert held is not None
    try:
        r = cli(*args)
    finally:
        held.close()
    assert r.returncode != 0
    assert DB(path).deal(5) is None


@pytest.mark.parametrize("args", [("once",), ("process", "5")])
def test_robot_commands_run_when_service_stopped(args):
    assert cli(*args).returncode == 0


@pytest.mark.parametrize("args", [("once",), ("process", "5")])
def test_robot_command_holds_database_while_it_works(args):
    """Разовая команда уже работает (ждёт ответа Битрикса) — сервис на ту же базу не запускается."""
    path = config.env().db_path
    db = DB(path)
    hang = socket.create_server(("127.0.0.1", 0))           # «Битрикс» принимает запрос и молчит
    port = hang.getsockname()[1]
    config.save(db, config.load(db), enabled=True, bitrix_webhook=f"http://127.0.0.1:{port}/rest/1/x/",
                no_messenger_stage="NO_MESSENGER", orbita_api_key="k", orbita_channels=["whatsapp"],
                window_start="06:00", window_end="18:00", tz_name=midday_tz())
    db.put("last_deal_id", 1)
    db.put("start_deal_id", 1)
    db.conn.close()
    hang.settimeout(20)
    proc = subprocess.Popen([sys.executable, "-m", "first_contact", *args],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        conn, _ = hang.accept()                               # команда дошла до работы с Битриксом
        try:
            held = service_lock(path)
            free = held is not None
            if held is not None:
                held.close()
        finally:
            conn.close()
    finally:
        hang.close()
        proc.wait(timeout=30)
    assert not free, "пока команда работает, замок базы занят"


def backup_with_marker(tmp_path, marker: str):
    """Копия базы, где в kv лежит marker."""
    copy = DB(tmp_path / "copy.sqlite3")
    copy.put("marker", marker)
    copy.conn.close()
    return tmp_path / "copy.sqlite3"


def test_restore_refuses_while_service_holds_database(tmp_path):
    """Сервис работает с базой: restore её не подменяет — иначе работающий робот продолжил бы на чужом состоянии
    (сделка, получившая первое сообщение после копии, снова «новая»)."""
    path = config.env().db_path
    live = DB(path)
    live.put("marker", "live")
    live.conn.close()
    copy = backup_with_marker(tmp_path, "copy")
    held = service_lock(path)
    try:
        r = cli("restore", str(copy))
    finally:
        held.close()
    assert r.returncode != 0
    assert DB(path).get("marker") == "live"


def test_restore_replaces_database_when_service_stopped(tmp_path):
    path = config.env().db_path
    DB(path).conn.close()
    r = cli("restore", str(backup_with_marker(tmp_path, "copy")))
    assert r.returncode == 0
    assert DB(path).get("marker") == "copy"
