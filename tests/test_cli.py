"""Командная строка (__main__.py) отдельным процессом: разовые команды и запущенный сервис."""

import subprocess
import sys

import pytest

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
