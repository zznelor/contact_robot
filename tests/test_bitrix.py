"""Клиент Битрикс24 REST (bitrix.py) на httpx.MockTransport: разбор сделки, постраничные списки."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import PHONE, run
from first_contact import config
from first_contact import bitrix as bitrix_mod
from first_contact.bitrix import Bitrix, BitrixError
from first_contact.orbita import FirstSendResult
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


def test_long_query_limit_pause_stays_within_30_minutes(http, monkeypatch):
    """Лимит держится сутки (соседи по IP выбирают его постоянно), успешных ответов нет: каждый отказ — ошибка лимита
    со сроком не дальше 30 минут (LIMIT_PAUSE_MAX в docs/РАЗРАБОТЧИКУ.md), а не сбой, тратящий попытки шага."""
    refusals = 60                                       # сутки по 30 минут — дальше, чем помещается удвоение минуты
    portal = Script(*[QUERY_LIMIT] * refusals)
    http(portal)
    shift = {"now": datetime.now(timezone.utc)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return shift["now"]
    monkeypatch.setattr(bitrix_mod, "datetime", Clock)
    b = Bitrix(HOOK)
    pauses = []
    for _ in range(refusals):
        with pytest.raises(BitrixError) as e:
            run(b.get_deal(7))
        pauses.append(e.value.retry_at - shift["now"])
        shift["now"] = e.value.retry_at
    assert len(portal.requests) == refusals
    assert max(pauses) == timedelta(minutes=30) and pauses[-1] == timedelta(minutes=30)
    # «нарастающая задержка 1, 2, 4 … минут, не больше 30» (docs/РАЗРАБОТЧИКУ.md)
    assert pauses[:7] == [timedelta(minutes=m) for m in (1, 2, 4, 8, 16, 30, 30)]


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
        self.contacts: dict[int, tuple[str, list[str]]] = {3: ("Анна", [PHONE])}   # ID → имя, телефоны
        self.bindings: list[tuple[int, int, str]] = [(3, 10, "Y")]   # привязки к сделке: контакт, SORT, IS_PRIMARY

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
            # CONTACT_ID «Устаревшее» (crm-deal-get.md); какой контакт в нём при нескольких — не описано: здесь главный
            primary = next((c for c, _, p in self.bindings if p == "Y"), None)
            return ok({**DEAL, "ID": str(body["id"]), "STAGE_ID": self.stage,
                       "CONTACT_ID": str(primary) if primary else None})
        if m == "crm.deal.contact.items.get":
            return ok([{"CONTACT_ID": c, "SORT": sort, "ROLE_ID": 0, "IS_PRIMARY": p} for c, sort, p in self.bindings])
        if m == "crm.contact.get":
            if body["id"] not in self.contacts:
                return error(400, "", "Not found")
            name, phones = self.contacts[body["id"]]
            return ok({"ID": str(body["id"]), "NAME": name,
                       "PHONE": [{"ID": str(n), "VALUE_TYPE": "WORK", "VALUE": v, "TYPE_ID": "PHONE"}
                                 for n, v in enumerate(phones)]})
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


# ---------- этапы воронки ----------

def status(status_id: str, name: str, semantics: str | None, sort: int) -> dict:
    """Строка crm.status.list (api-reference/crm/status/crm-status-list.md): SEMANTICS S — успешная, F — провальная,
    null — в работе."""
    return {"ID": str(sort), "ENTITY_ID": "DEAL_STAGE", "STATUS_ID": status_id, "NAME": name, "NAME_INIT": name,
            "SORT": str(sort), "SYSTEM": "N", "CATEGORY_ID": None, "COLOR": "#39A8EF", "SEMANTICS": semantics,
            "EXTRA": {"SEMANTICS": {"S": "success", "F": "failure"}.get(semantics, "process"), "COLOR": "#39A8EF"}}


# Пример ответа из статьи crm.status.list: пять этапов в работе, успех, провал и «Анализ причины провала» (тоже F)
DOC_STAGES = [status("NEW", "Новая", None, 10), status("PREPARATION", "Подготовка документов", None, 20),
              status("PREPAYMENT_INVOICE", "Cчет на предоплату", None, 30), status("EXECUTING", "В работе", None, 40),
              status("FINAL_INVOICE", "Финальный счет", None, 50), status("WON", "Сделка успешна", "S", 60),
              status("LOSE", "Сделка провалена", "F", 70), status("APOLOGY", "Анализ причины провала", "F", 80)]


def test_stages_only_in_progress(http):
    """Этап «Не нашёл мессенджер» выбирается только из этапов в работе: закрывающий закрыл бы сделку, и «Повторить»
    её не вернул бы (робот пропускает закрытые сделки)."""
    http(lambda req: ok(DOC_STAGES))
    assert [i for i, _ in run(Bitrix(HOOK).stages("0"))] == [
        "NEW", "PREPARATION", "PREPAYMENT_INVOICE", "EXECUTING", "FINAL_INVOICE"]


# ---------- телефон — из всех контактов сделки ----------

def test_deal_contacts_primary_first_then_by_sort(http):
    """crm.deal.contact.items.get: все контакты сделки, IS_PRIMARY и SORT (CONTACT_ID сделки — «Устаревшее»)."""
    portal = Portal()
    portal.contacts = {54: ("Борис", ["8 (999) 000-00-02"]), 55: ("Анна", [PHONE, PHONE]), 56: ("Вера", [])}
    portal.bindings = [(54, 100, "N"), (55, 200, "Y"), (56, 50, "N")]
    http(portal)
    contacts = run(Bitrix(HOOK).deal_contacts(7))
    assert [(c.name, c.phones) for c in contacts] == [("Анна", [PHONE]), ("Вера", []), ("Борис", ["+79990000002"])]


def only_number(orbita, number):
    """Подделка Орбиты: мессенджер есть только у этого номера."""
    real = orbita.start_dialog

    async def start(channel, phone, text, key, name=None):
        if phone != number:
            orbita.keys.append(key)
            return FirstSendResult("not_found", channel=channel, raw="not_found")
        return await real(channel, phone, text, key, name)
    orbita.start_dialog = start


def test_phone_of_second_contact_used_with_its_name(portal_robot, orbita):
    """У главного контакта телефона нет, у других есть: номера перебираются по порядку контактов, без дублей; имя в
    сообщении — того контакта, чей номер ушёл; ключ идемпотентности — с номером, как раньше."""
    portal = Portal()
    portal.contacts = {3: ("Директор", []), 4: ("Анна", ["+79990000002", PHONE]), 5: ("Борис", [PHONE, "+79990000003"])}
    portal.bindings = [(5, 30, "N"), (3, 10, "Y"), (4, 20, "N")]
    w = portal_robot(portal)
    w.s.tpl_first, w.s.orbita_channels = "{name}: {deal_title}", ["whatsapp"]
    only_number(orbita, "+79990000003")
    portal.new_ids = [104]
    run(w.tick())
    row = w.db.deal(104)
    assert orbita.keys == [f"deal-104-first-{p}-whatsapp" for p in ("+79990000002", PHONE, "+79990000003")]
    assert orbita.first == [("+79990000003", "Борис: Заявка", "deal-104-first-+79990000003-whatsapp")]
    assert (row["status"], row["name"], row["phone"]) == ("sent_first", "Борис", "+79990000003")


def test_duplicate_number_named_after_first_contact_with_it(portal_robot, orbita):
    portal = Portal()
    portal.contacts = {3: ("Анна", [PHONE]), 4: ("Борис", [PHONE])}
    portal.bindings = [(4, 20, "N"), (3, 10, "Y")]
    w = portal_robot(portal)
    w.s.tpl_first, w.s.orbita_channels = "{name}", ["whatsapp"]
    portal.new_ids = [105]
    run(w.tick())
    assert orbita.first == [(PHONE, "Анна", f"deal-105-first-{PHONE}-whatsapp")]


def test_lost_answer_repeats_with_name_of_that_number(portal_robot, orbita, clock):
    """Ответ на отправку номеру второго контакта потерян: повтор — тем же телом, с именем этого контакта."""
    portal = Portal()
    portal.contacts = {3: ("Анна", ["+79990000002"]), 4: ("Борис", [PHONE])}
    portal.bindings = [(3, 10, "Y"), (4, 20, "N")]
    w = portal_robot(portal)
    w.s.tpl_first, w.s.orbita_channels = "{name}", ["whatsapp"]
    only_number(orbita, PHONE)
    orbita.answers["whatsapp"] = "lost"
    portal.new_ids = [106]
    run(w.tick())
    orbita.answers.clear()
    clock.advance(minutes=3)                            # пауза после сбоя
    run(w.tick())
    key = f"deal-106-first-{PHONE}-whatsapp"
    assert orbita.first == [(PHONE, "Борис", key), (PHONE, "Борис", key)]
    assert w.db.deal(106)["status"] == "sent_first"


# ---------- неверный адрес портала ----------

def test_portal_moved_shows_new_address(http):
    """После смены адреса облачный Битрикс24 отвечает 301/302 с новым адресом в Location
    (settings/how-to-call-rest-api/change-domen.md): владелец видит, куда переехал портал."""
    new = "https://new-portal.bitrix24.ru/rest/1/x/crm.deal.get.json"
    http(lambda req: httpx.Response(301, headers={"Location": new}, text="<html>Moved</html>"))
    with pytest.raises(BitrixError) as e:
        run(Bitrix(HOOK).get_deal(7))
    assert "https://new-portal.bitrix24.ru" in str(e.value)
    # путь вебхука с его кодом (секретом) в текст ошибки — в журнал и панель — не попадает
    assert "/rest/" not in str(e.value)


def test_portal_unreachable_is_bitrix_error(http):
    """Адрес портала не резолвится или порт закрыт — ошибка Битрикса с адресом портала, а не сырой сбой httpx."""
    def refuse(req):
        raise httpx.ConnectError("[Errno -2] Name or service not known", request=req)
    http(refuse)
    with pytest.raises(BitrixError) as e:
        run(Bitrix(HOOK).get_deal(7))
    assert "p.bitrix24.ru" in str(e.value)


# ---------- запрос к порталу: адрес, метод и параметры по статьям методов ----------

class Recorder:
    """Портал: answer(req) — ответ на каждый запрос; reqs — запросы целиком (адрес, тело, таймаут). Больше limit
    запросов — клиент не остановился (без этого ошибка в условии выхода из цикла повесила бы тест)."""

    def __init__(self, answer, limit=10):
        self.answer, self.limit, self.reqs = answer, limit, []

    def __call__(self, req):
        self.reqs.append(req)
        if len(self.reqs) > self.limit:
            raise AssertionError(f"больше {self.limit} запросов к порталу: клиент не остановился")
        return self.answer(req)

    @property
    def sent(self) -> list[tuple[str, dict]]:
        return [(method_of(r), json.loads(r.content)) for r in self.reqs]


def frozen(monkeypatch, at: datetime) -> dict:
    """Часы клиента Битрикса стоят на at; now["at"] переводит их."""
    now = {"at": at}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now["at"]
    monkeypatch.setattr(bitrix_mod, "datetime", Clock)
    return now


@pytest.mark.parametrize("webhook", [HOOK, HOOK.rstrip("/")])
def test_method_called_at_webhook_address(http, webhook):
    """Метод вызывается POST на <адрес вебхука><метод>.json (how-to-call-rest-api/authorization.md:
    https://портал/rest/1/<код>/crm.item.add.json) с параметрами в теле; адрес из настроек — со слешем и без."""
    portal = Recorder(lambda req: ok(DEAL))
    http(portal)
    run(Bitrix(webhook).get_deal(7))
    (req,) = portal.reqs
    assert (req.method, str(req.url)) == ("POST", "https://p.bitrix24.ru/rest/1/x/crm.deal.get.json")
    assert json.loads(req.content) == {"id": 7}


def test_timeout_from_settings_on_every_request(http):
    """Таймаут клиента доходит до запроса: портал, который не отвечает, не держит проход робота бесконечно."""
    portal = Recorder(lambda req: ok(DEAL))
    http(portal)
    run(Bitrix(HOOK, timeout=7).get_deal(7))
    assert portal.reqs[0].extensions["timeout"] == {"connect": 7, "read": 7, "write": 7, "pool": 7}


@pytest.mark.parametrize("semantic, category", [("P", "0"), ("S", "3"), ("F", "3")])
def test_deal_fields_from_doc_example(http, semantic, category):
    """Поля ответа crm.deal.get (пример статьи deals/crm-deal-get.md): STAGE_SEMANTIC_ID — P в работе, S успешная,
    F неуспешная; CATEGORY_ID — воронка; DATE_CREATE со смещением."""
    http(lambda req: ok({"ID": "43", "TITLE": "Новая сделка #1", "STAGE_ID": "PREPARATION", "CONTACT_ID": "84",
                         "DATE_CREATE": "2024-08-30T14:29:00+02:00", "CATEGORY_ID": category,
                         "STAGE_SEMANTIC_ID": semantic, "CLOSED": "N"}))
    d = run(Bitrix(HOOK).get_deal(43))
    assert (d.title, d.stage_id, d.stage_semantic, d.category_id, d.created_at) == (
        "Новая сделка #1", "PREPARATION", semantic, category, datetime(2024, 8, 30, 12, 29, tzinfo=timezone.utc))


@pytest.mark.parametrize("ids, expected", [(range(101, 161), 160), ([], 0)])
def test_max_deal_id_is_newest_deal(http, ids, expected):
    """Последняя сделка портала — первая строка crm.deal.list с сортировкой ID DESC и start=-1 (без подсчёта total,
    settings/performance/huge-data.md); сделок нет — 0."""
    def answer(req):
        body = json.loads(req.content)
        rows = sorted(ids, reverse=body.get("order", {}).get("ID") == "DESC")
        start = int(body.get("start", 0))
        return ok([{"ID": str(i)} for i in (rows[:50] if start == -1 else rows[start:start + 50])])
    portal = Recorder(answer)
    http(portal)
    assert run(Bitrix(HOOK).max_deal_id()) == expected
    assert portal.sent == [("crm.deal.list", {"select": ["ID"], "order": {"ID": "DESC"}, "start": -1})]


@pytest.mark.parametrize("category, entity", [(None, "DEAL_STAGE"), ("0", "DEAL_STAGE"), ("", "DEAL_STAGE"),
                                              ("3", "DEAL_STAGE_3")])
def test_stages_of_funnel_requested_by_entity(http, category, entity):
    """Этапы основной воронки — ENTITY_ID DEAL_STAGE (crm-status-list.md), воронки N — DEAL_STAGE_N
    («DEAL_STAGE_xx — стадии сделки в воронках не по умолчанию», crm-status-add.md), по SORT."""
    portal = Recorder(lambda req: ok(DOC_STAGES))
    http(portal)
    run(Bitrix(HOOK).stages(category))
    assert portal.sent == [("crm.status.list", {"filter": {"ENTITY_ID": entity}, "order": {"SORT": "ASC"},
                                                "start": 0})]


# Воронки из примера ответа crm.category.list (universal/category/crm-category-list.md), основная — id 0
DOC_CATEGORIES = [
    {"id": 9, "name": "Воронка с оригинальным названием", "sort": 200, "entityTypeId": 2, "isDefault": "N",
     "originId": "", "originatorId": ""},
    {"id": 10, "name": "Лидовый маршрут", "sort": 200, "entityTypeId": 2, "isDefault": "N", "originId": "",
     "originatorId": ""},
    {"id": 0, "name": "Общая", "sort": 300, "entityTypeId": 2, "isDefault": "Y"},
]


DOC_NAMES = {str(c["id"]): c["name"] for c in DOC_CATEGORIES}


@pytest.mark.parametrize("answer, ids, names", [
    (ok({"categories": DOC_CATEGORIES}), ["9", "10", "0"], DOC_NAMES),
    # «Список отдаваемых воронок фильтруется по правам доступа»: основной в ответе нет — она всё равно в списке, первой
    (ok({"categories": DOC_CATEGORIES[:2]}), ["0", "9", "10"], {"9": DOC_NAMES["9"], "10": DOC_NAMES["10"]}),
    (error(401, "insufficient_scope", "The request requires higher privileges than provided by the webhook token"),
     ["0"], {}),
])
def test_categories_of_deals(http, answer, ids, names):
    """Воронки сделок — crm.category.list с entityTypeId 2 (сделка), названия из ответа; основная воронка «0» есть
    в выборе всегда — и когда портал её не отдал, и когда отказал."""
    portal = Recorder(lambda req: answer)
    http(portal)
    got = run(Bitrix(HOOK).categories())
    assert [i for i, _ in got] == ids
    assert {i: n for i, n in got if i in names} == names
    assert portal.sent == [("crm.category.list", {"entityTypeId": 2})]


@pytest.mark.parametrize("act, method, params", [
    # crm.timeline.comment.add: «принимает только параметры с ключом fields в нижнем регистре», ENTITY_TYPE "deal"
    (lambda b: b.comment(7, "Написал клиенту"), "crm.timeline.comment.add",
     {"fields": {"ENTITY_ID": 7, "ENTITY_TYPE": "deal", "COMMENT": "Написал клиенту"}}),
    # crm.deal.update: id и fields со STAGE_ID
    (lambda b: b.set_stage(7, "NO_MESSENGER"), "crm.deal.update", {"id": 7, "fields": {"STAGE_ID": "NO_MESSENGER"}}),
])
def test_deal_writes_by_doc(http, act, method, params):
    portal = Recorder(lambda req: ok(True))
    http(portal)
    run(act(Bitrix(HOOK)))
    assert portal.sent == [(method, params)]


def test_deal_contacts_request_and_equal_sort_keep_answer_order(http):
    """crm.deal.contact.items.get с id сделки; у привязок с одинаковым SORT порядок — как в ответе портала."""
    portal = Portal()
    portal.contacts = {54: ("Борис", []), 55: ("Анна", []), 56: ("Вера", [])}
    portal.bindings = [(54, 100, "N"), (56, 100, "N"), (55, 200, "Y")]
    sent = []

    def recording(req):
        sent.append((method_of(req), json.loads(req.content)))
        return portal(req)
    http(recording)
    assert [c.name for c in run(Bitrix(HOOK).deal_contacts(7))] == ["Анна", "Борис", "Вера"]
    assert sent[0] == ("crm.deal.contact.items.get", {"id": 7})


def unreadable_first_contact(http, answer) -> Portal:
    """Сделка с двумя контактами; crm.contact.get главного отвечает answer (contacts/crm-contact-get.md: «Not found»,
    «Access denied» с пустым кодом)."""
    portal = Portal()
    portal.contacts = {55: ("Анна", [PHONE])}
    portal.bindings = [(54, 10, "Y"), (55, 20, "N")]
    portal.fail["crm.contact.get"] = [answer]
    http(portal)
    return portal


def test_deleted_contact_skipped(http):
    """Контакт удалили между запросами — привязки к нему уже нет: остальные контакты сделки читаются."""
    unreadable_first_contact(http, error(400, "", "Not found"))
    assert [c.name for c in run(Bitrix(HOOK).deal_contacts(7))] == ["Анна"]


def test_contact_without_access_is_error(http):
    """Нет прав на контакт — ошибка, которую повтор не исправит, а не сделка без телефона этого контакта."""
    unreadable_first_contact(http, error(400, "", "Access denied"))
    with pytest.raises(BitrixError) as e:
        run(Bitrix(HOOK).deal_contacts(7))
    assert e.value.permanent and not e.value.not_found


@pytest.mark.parametrize("answer, missing, permanent", [
    (error(400, "", "Not found."), True, True),            # пример ответа статьи crm.deal.contact.items.get
    (error(400, "", "Access denied."), False, True),       # «Пустое значение | Access denied.» — там же
    (error(400, "ACCESS_DENIED", "Access denied!"), False, True),
])
def test_deal_contacts_errors_from_doc(http, answer, missing, permanent):
    """Ошибки crm.deal.contact.items.get дословно из статьи метода — с точкой и восклицательным знаком."""
    http(Script(answer))
    with pytest.raises(BitrixError) as e:
        run(Bitrix(HOOK).deal_contacts(7))
    assert (e.value.deal_missing, e.value.permanent) == (missing, permanent)


def test_contact_phones_normalized(http):
    """Телефоны контакта (мультиполе PHONE, contacts/crm-contact-get.md) — к виду +7…; российский мобильный без кода
    страны (10 цифр на 9) получает +7 при любой второй цифре."""
    http(lambda req: ok({"ID": "3", "NAME": "Анна", "PHONE": [
        {"ID": "156", "VALUE_TYPE": "WORK", "VALUE": "916 123-45-67", "TYPE_ID": "PHONE"},
        {"ID": "157", "VALUE_TYPE": "HOME", "VALUE": "+35599888666", "TYPE_ID": "PHONE"}]}))
    assert run(Bitrix(HOOK).get_contact(3)).phones == ["+79161234567", "+35599888666"]


# ---------- ответы, по которым клиент не должен зациклиться или упасть не той ошибкой ----------

def test_list_stops_when_next_does_not_advance(http):
    """next не дальше текущего смещения — список кончился: иначе клиент запрашивал бы ту же страницу без конца."""
    rows = [{"STATUS_ID": f"S{i}", "NAME": f"Этап {i}"} for i in range(60)]

    def answer(req):
        start = json.loads(req.content)["start"]
        return httpx.Response(200, json={"result": rows[start:start + 50], "next": 50, "total": len(rows)})
    portal = Recorder(answer, limit=3)
    http(portal)
    assert len(run(Bitrix(HOOK).stages("0"))) == 60
    assert [body["start"] for _, body in portal.sent] == [0, 50]


def test_new_deals_without_filter_applied_is_error(http):
    """Полная страница crm.deal.list без сделок после курсора (фильтр >ID не применён) — ошибка Битрикса, а не
    бесконечный опрос той же страницы."""
    portal = Recorder(lambda req: ok([{"ID": str(i)} for i in range(1, 51)]), limit=3)
    http(portal)
    with pytest.raises(BitrixError):
        run(Bitrix(HOOK).deal_ids_after(50, None))
    assert len(portal.reqs) == 1


@pytest.mark.parametrize("answer", [
    httpx.Response(200, text="<html>Вход в Битрикс24</html>"),
    httpx.Response(200, json={"status": "ok"}),           # JSON, но не ответ REST: ни result, ни error
    httpx.Response(200, json=["ok"]),
    httpx.Response(302, text="<html>Found</html>"),       # переадресация без Location
])
def test_not_bitrix_answer_is_bitrix_error(http, answer):
    """Адрес вебхука ведёт не на Битрикс24 REST — ошибка Битрикса (её видит владелец), а не сбой разбора ответа."""
    http(lambda req: answer)
    with pytest.raises(BitrixError):
        run(Bitrix(HOOK).get_deal(7))


# ---------- ошибки лимитов: срок и область блокировки ----------

def test_operation_time_limit_waits_for_reset_from_this_answer(http):
    """Срок OPERATION_TIME_LIMIT — time.operating_reset_at из этого ответа, если он есть (docs/РАЗРАБОТЧИКУ.md);
    успешных ответов метода до него не было."""
    reset = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=7)
    http(Script(error(429, "OPERATION_TIME_LIMIT", "Method is blocked due to operation time limit",
                      time={"start": 1, "finish": 2, "operating_reset_at": reset.timestamp(), "operating": 480.5})))
    with pytest.raises(BitrixError) as e:
        run(Bitrix(HOOK).get_deal(7))
    assert e.value.retry_at == reset


def test_operation_time_limit_blocks_only_that_method(http):
    """OPERATION_TIME_LIMIT блокирует один метод: «Другие приложения, вебхуки и методы продолжают работать»
    (settings/performance/limits.md). До срока сам метод в портал не ходит, другие ходят."""
    portal = Script(TIME_LIMIT, ok(1))
    http(portal)
    b = Bitrix(HOOK)
    with pytest.raises(BitrixError):
        run(b.get_deal(7))
    run(b.comment(7, "x"))
    with pytest.raises(BitrixError):
        run(b.get_deal(7))
    assert portal.requests == ["crm.deal.get", "crm.timeline.comment.add"]


def test_operation_time_limit_when_reset_reached_waits_increasing_pause(http, monkeypatch):
    """Ошибка пришла в момент operating_reset_at последнего успешного ответа — срок прошёл, а метод всё ещё
    заблокирован: ждём нарастающую паузу (минута), а не уже наступивший срок (docs/РАЗРАБОТЧИКУ.md)."""
    t0 = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    now = frozen(monkeypatch, t0)
    http(Script(ok(DEAL, (t0 + timedelta(minutes=5)).timestamp()), TIME_LIMIT))
    b = Bitrix(HOOK)
    run(b.get_deal(7))
    now["at"] = t0 + timedelta(minutes=5)
    with pytest.raises(BitrixError) as e:
        run(b.get_deal(7))
    assert e.value.retry_at == t0 + timedelta(minutes=6)
