"""Клиент Битрикс24 REST (bitrix.py) на httpx.MockTransport: разбор сделки, постраничные списки."""

from datetime import datetime, timezone

import httpx
import pytest

from conftest import run
from first_contact.bitrix import Bitrix

HOOK = "https://p.bitrix24.ru/rest/1/x/"


def method_of(req: httpx.Request) -> str:
    return req.url.path.rsplit("/", 1)[-1].removesuffix(".json")


@pytest.mark.parametrize("date_create, expected", [
    ("2026-10-21T13:00:00+03:00", datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)),
    ("2026-10-21T13:00:00", datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc)),   # без смещения — UTC
    ("", None),
    ("вчера", None),
])
def test_deal_creation_time(http, date_create, expected):
    http(lambda req: httpx.Response(200, json={"result": {
        "ID": "7", "TITLE": "t", "STAGE_ID": "NEW", "CATEGORY_ID": "0", "DATE_CREATE": date_create}}))
    assert run(Bitrix(HOOK).get_deal(7)).created_at == expected
