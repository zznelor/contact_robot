"""Клиент Битрикс24 REST (bitrix.py) на httpx.MockTransport: разбор сделки, постраничные списки."""

import json
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


def paged(rows, page=50):
    """Списочный метод Битрикса: по 50 строк, next — откуда следующая страница, как в REST."""
    calls = []

    def handler(req):
        start = int(json.loads(req.content).get("start") or 0)
        calls.append(start)
        body = {"result": rows[start:start + page], "total": len(rows)}
        if start + page < len(rows):
            body["next"] = start + page
        return httpx.Response(200, json=body)
    return handler, calls


def test_stages_all_pages(http):
    handler, _ = paged([{"STATUS_ID": f"S{i}", "NAME": f"Этап {i}"} for i in range(60)])
    http(handler)
    stages = run(Bitrix(HOOK).stages("0"))
    assert len(stages) == 60 and stages[-1] == ("S59", "Этап 59")


def test_new_deals_all_pages(http):
    handler, _ = paged([{"ID": str(i)} for i in range(101, 154)])
    http(handler)
    assert run(Bitrix(HOOK).deal_ids_after(100, "0")) == list(range(101, 154))
