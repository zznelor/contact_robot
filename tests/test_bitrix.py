"""Клиент Битрикс24 REST (bitrix.py) на httpx.MockTransport: разбор сделки, постраничные списки."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import PHONE, run
from first_contact import config
from first_contact import bitrix as bitrix_mod
from first_contact.bitrix import Bitrix, BitrixError
from first_contact.worker import Worker

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


# ---------- ошибки Битрикса: лимиты и то, что повтором не исправить ----------

def error(status: int, code: str, description: str, **extra) -> httpx.Response:
    """Ошибка REST по документации: {"error", "error_description"} (_includes/error-info.md); у ошибок уровня метода
    код пустой, причина — только в описании."""
    return httpx.Response(status, json={"error": code, "error_description": description, **extra})


QUERY_LIMIT = error(503, "QUERY_LIMIT_EXCEEDED", "Too many requests")
TIME_LIMIT = error(429, "OPERATION_TIME_LIMIT", "Method is blocked due to operation time limit")


def ok(result, reset_at: float | None = None) -> httpx.Response:
    time = {"start": 1, "finish": 2, "duration": 1, "processing": 1}
    if reset_at is not None:
        time.update(operating_reset_at=reset_at, operating=3.3)
    return httpx.Response(200, json={"result": result, "time": time})


DEAL = {"ID": "7", "TITLE": "Заявка", "STAGE_ID": "NEW", "STAGE_SEMANTIC_ID": "P", "CATEGORY_ID": "0",
        "CONTACT_ID": "3"}


class Script:
    """Ответы портала по очереди; requests — вызванные методы."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, req):
        self.requests.append(method_of(req))
        return self.answers.pop(0)


def test_query_limit_pauses_all_calls(http):
    """QUERY_LIMIT_EXCEEDED — лимит интенсивности на весь портал (settings/performance/limits.md): до названного
    срока клиент не шлёт в Битрикс ни одного запроса, в том числе другим методом."""
    portal = Script(QUERY_LIMIT)
    http(portal)
    b = Bitrix(HOOK)
    with pytest.raises(BitrixError) as first:
        run(b.get_deal(7))
    t1 = first.value.retry_at
    assert t1 is not None and t1 > datetime.now(timezone.utc)
    with pytest.raises(BitrixError) as blocked:
        run(b.comment(7, "x"))
    assert blocked.value.retry_at == t1 and len(portal.requests) == 1, "до срока — ни одного запроса"


def test_query_limit_pause_increases_and_resets(http, monkeypatch):
    """«Повторять вызовы с увеличивающейся задержкой»: повторный отказ — пауза дольше, успешный ответ сбрасывает рост."""
    portal = Script(QUERY_LIMIT, QUERY_LIMIT, ok(DEAL), QUERY_LIMIT)
    http(portal)
    shift = {"now": datetime.now(timezone.utc)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return shift["now"]
    monkeypatch.setattr(bitrix_mod, "datetime", Clock)
    b = Bitrix(HOOK)
    pauses = []
    for expect_ok in (False, False, True, False):
        if expect_ok:
            run(b.get_deal(7))
            continue
        with pytest.raises(BitrixError) as e:
            run(b.get_deal(7))
        pauses.append(e.value.retry_at - shift["now"])
        shift["now"] = e.value.retry_at
    assert pauses[1] > pauses[0], "повторный отказ — пауза дольше"
    assert pauses[2] == pauses[0], "после успешного ответа — снова с короткой паузы"


def test_operation_time_limit_waits_for_reset_from_last_success(http):
    """OPERATION_TIME_LIMIT: «для первой повторной попытки ориентироваться на operating_reset_at из последнего
    успешного ответа» этого метода (limits.md)."""
    reset = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=7)
    portal = Script(ok(DEAL, reset.timestamp()), TIME_LIMIT)
    http(portal)
    b = Bitrix(HOOK)
    run(b.get_deal(7))
    with pytest.raises(BitrixError) as e:
        run(b.get_deal(7))
    assert e.value.retry_at == reset


@pytest.mark.parametrize("answer, permanent", [
    (error(400, "", "Not found"), True),
    (error(400, "", "Access denied"), True),
    (error(400, "ACCESS_DENIED", "Access denied!"), True),
    (error(401, "INVALID_CREDENTIALS", "Invalid request credentials"), True),
    (error(401, "insufficient_scope", "The request requires higher privileges than provided by the webhook token"),
     True),
    (error(404, "ERROR_METHOD_NOT_FOUND", "Method not found!"), True),
    (error(500, "INTERNAL_SERVER_ERROR", "Internal server error"), False),
    (QUERY_LIMIT, False),
])
def test_errors_that_retry_cannot_fix(http, answer, permanent):
    """Ошибки, которые повтором не исправить (_includes/system-errors.md, «Возможные коды ошибок» crm.deal.get)."""
    http(Script(answer))
    with pytest.raises(BitrixError) as e:
        run(Bitrix(HOOK).get_deal(7))
    assert e.value.permanent is permanent


# ---------- робот на настоящем клиенте Битрикса ----------

class Portal:
    """Портал Битрикса по документации методов: сделки 1..max_id в воронке «0» на этапе NEW, у каждой — контакт 3 с
    телефоном. fail[метод] — ответы вместо обычного, по очереди. calls — вызванные методы."""

    def __init__(self, max_id=100):
        self.max_id, self.new_ids = max_id, []
        self.fail: dict[str, list[httpx.Response]] = {}
        self.calls: list[str] = []
        self.stage = "NEW"

    def __call__(self, req):
        m = method_of(req)
        body = json.loads(req.content or b"{}")
        self.calls.append(m)
        if self.fail.get(m):
            return self.fail[m].pop(0)
        if m == "crm.deal.list":
            if body.get("order") == {"ID": "DESC"}:
                return ok([{"ID": str(self.max_id)}])
            return ok([{"ID": str(i)} for i in self.new_ids if i > int(body["filter"][">ID"])][:50])
        if m == "crm.deal.get":
            return ok({**DEAL, "ID": str(body["id"]), "STAGE_ID": self.stage})
        if m == "crm.deal.contact.items.get":
            return ok([{"CONTACT_ID": 3, "SORT": 10, "ROLE_ID": 0, "IS_PRIMARY": "Y"}])
        if m == "crm.contact.get":
            return ok({"ID": "3", "NAME": "Анна", "PHONE": [{"ID": "1", "VALUE_TYPE": "WORK", "VALUE": PHONE,
                                                             "TYPE_ID": "PHONE"}]})
        if m == "crm.deal.update":
            self.stage = body["fields"]["STAGE_ID"]
            return ok(True)
        if m == "crm.timeline.comment.add":
            return ok(1)
        raise AssertionError(m)


@pytest.fixture
def portal_robot(db, settings, orbita, http):
    def make(portal: Portal) -> Worker:
        http(portal)
        w = Worker(config.env(), settings, db, Bitrix(HOOK), orbita)
        run(w.enable())
        return w
    return make


def test_rate_limit_does_not_spend_attempts(portal_robot, orbita, clock):
    """Лимит Битрикса держится дольше пяти обычных попыток: сделка ждёт, попытки не тратятся, в «ошибку» не уходит;
    лимит снят — первое сообщение уходит."""
    portal = Portal()
    w = portal_robot(portal)
    portal.new_ids = [101]
    portal.fail["crm.deal.get"] = [QUERY_LIMIT] * 4 + [TIME_LIMIT] * 4
    for _ in range(8):
        run(w.tick())
        row = w.db.deal(101)
        assert (row["status"], row["attempts"]) == ("new", 0)
        clock.advance(minutes=31)                       # пауза лимита — не дольше 30 мин
    run(w.tick())
    assert w.db.deal(101)["status"] == "sent_first"


@pytest.mark.parametrize("answer, status", [
    (error(400, "", "Access denied"), "error"),
    (error(401, "INVALID_CREDENTIALS", "Invalid request credentials"), "error"),
    (error(400, "", "Not found"), "skipped"),            # сделку удалили в Битриксе
])
def test_error_retry_cannot_fix_is_final_at_once(portal_robot, orbita, answer, status):
    portal = Portal()
    w = portal_robot(portal)
    portal.new_ids = [102]
    portal.fail["crm.deal.get"] = [answer]
    run(w.tick())
    row = w.db.deal(102)
    assert row["status"] == status and row["last_error"]
    assert portal.calls.count("crm.deal.get") == 1 and orbita.keys == []


def test_stage_return_waits_out_rate_limit_without_attempts(portal_robot, orbita, clock):
    """Возврат этапа после «Повторить» упёрся в лимит: ждёт его, счётчик сбоев возврата не растёт."""
    portal = Portal()
    w = portal_robot(portal)
    orbita.mode = "none"
    portal.new_ids = [103]
    run(w.tick())
    assert w.db.deal(103)["status"] == "no_messenger" and portal.stage == "NO_MESSENGER"
    orbita.mode = "sent"
    run(w.retry(103, "a@x.ru"))
    portal.fail["crm.deal.update"] = [QUERY_LIMIT] * 6
    for _ in range(6):
        run(w.tick())
        clock.advance(minutes=31)
    row = w.db.deal(103)
    assert row["first_sent_at"] and row["stage_before"] == "NEW" and row["stage_attempts"] == 0
    run(w.tick())
    assert portal.stage == "NEW" and w.db.deal(103)["stage_before"] is None
