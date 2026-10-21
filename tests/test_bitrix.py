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



class DealList:
    """crm.deal.list по документации (settings/performance/huge-data.md, list-methods-pecularities.md): фильтр >ID и
    CATEGORY_ID, сортировка ID ASC, страница — 50 строк. start=-1 — до 50 строк без подсчёта total и без next;
    start>=0 — смещение, next — откуда следующая страница. after_first — что поменялось в портале после первого
    запроса (удалили сделку, отобрали права)."""

    def __init__(self, ids, after_first=None):
        self.ids, self.after_first, self.requests = list(ids), after_first, []

    def __call__(self, req):
        assert method_of(req) == "crm.deal.list"
        body = json.loads(req.content)
        self.requests.append(body)
        rows = sorted(i for i in self.ids if i > int(body["filter"].get(">ID", 0)))
        start = int(body.get("start", 0))
        if start == -1:
            page, out = rows[:50], {"total": 0}
        else:
            page, out = rows[start:start + 50], {"total": len(rows)}
            if start + 50 < len(rows):
                out["next"] = start + 50
        if len(self.requests) == 1 and self.after_first:
            self.after_first(self.ids)
        return httpx.Response(200, json={"result": [{"ID": str(i)} for i in page], **out})


def test_new_deals_survive_deletion_between_pages(http):
    """Сделку с первой страницы удалили, пока робот читал вторую: следующая за страницей сделка не теряется
    (по смещению start=50 она оказалась бы первой строкой «прочитанной» страницы и выпала навсегда)."""
    portal = DealList(range(101, 161), after_first=lambda ids: ids.remove(101))
    http(portal)
    assert run(Bitrix(HOOK).deal_ids_after(100, "0")) == list(range(101, 161))
    assert [r["start"] for r in portal.requests] == [-1, -1], "без подсчёта total, страница — фильтром >ID"
    assert [r["filter"][">ID"] for r in portal.requests] == [100, 150]
    assert all(r["order"] == {"ID": "ASC"} and r["filter"]["CATEGORY_ID"] == "0" for r in portal.requests)


@pytest.mark.parametrize("count, requests", [(0, 1), (49, 1), (50, 2), (100, 3)])
def test_new_deals_read_until_short_page(http, count, requests):
    """Повторять, пока метод не вернёт меньше 50 элементов (huge-data.md)."""
    portal = DealList(range(101, 101 + count))
    http(portal)
    assert run(Bitrix(HOOK).deal_ids_after(100, "0")) == list(range(101, 101 + count))
    assert len(portal.requests) == requests
