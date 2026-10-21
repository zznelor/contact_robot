"""Битрикс24 REST через входящий вебхук."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx


# Размер страницы списочных методов: «всегда статичный — 50 записей» (list-methods-pecularities.md)
PAGE = 50


# Ошибки лимитов (_includes/system-errors.md, settings/performance/limits.md): повтор поможет, но позже, и попытку
# он не тратит. QUERY_LIMIT_EXCEEDED — интенсивность запросов ко всему порталу (общая для приложений с одного IP):
# «повторять вызовы с увеличивающейся задержкой». OPERATION_TIME_LIMIT — ресурсоёмкость одного метода для вебхука:
# пауза до operating_reset_at последнего успешного ответа этого метода, нет его — та же нарастающая задержка
LIMIT_CODES = {"QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT"}
# Задержка после отказа по лимиту: 1, 2, 4 … мин, не больше 30. Счётчик опустошается на 2 запроса в секунду при
# пороге 50 (тариф «Прочие», limits.md) — минута с запасом; рост — на случай соседей по IP с тем же порталом
LIMIT_PAUSE = timedelta(minutes=1)
LIMIT_PAUSE_MAX = timedelta(minutes=30)
# Повтор не поможет: нет доступа, вебхук не найден или без нужных прав, метода нет, портал закрыт
# (_includes/system-errors.md; NOT_FOUND / ACCESS_DENIED — crm.timeline.comment.add, crm.deal.contact.items.get).
# OVERLOAD_LIMIT — «ручная индивидуальная блокировка», снимает только поддержка Битрикса, ожиданием не проходит
PERMANENT_CODES = {"NO_AUTH_FOUND", "INVALID_REQUEST", "OVERLOAD_LIMIT", "ACCESS_DENIED", "INVALID_CREDENTIALS",
                   "ERROR_METHOD_NOT_FOUND", "insufficient_scope", "expired_token", "user_access_error",
                   "PORTAL_DELETED", "NOT_FOUND"}
# Ошибки уровня метода приходят с пустым кодом, причина — только в error_description (_includes/error-info.md):
# «Not found» и «Access denied» у crm.deal.get / update, crm.contact.get, crm.deal.contact.items.get. Другой текст
# (например, перевод) — обычный сбой с повторами: так безопаснее, чем принять сбой за окончательный
NOT_FOUND_TEXTS = {"not found"}
ACCESS_DENIED_TEXTS = {"access denied"}
# SEMANTICS закрывающих этапов (crm.status.list): S — успешная, F — провальная
CLOSED = {"S", "F"}
# Методы, чьё «Not found» значит: сделки нет (удалили)
DEAL_METHODS = {"crm.deal.get", "crm.deal.update", "crm.deal.contact.items.get"}


def _text(description) -> str:
    return str(description or "").strip().rstrip(".!").strip().lower()


class BitrixError(RuntimeError):
    """Отказ или сбой Битрикса. retry_at — ошибка лимита: раньше этого срока вызывать бессмысленно (попытку шага не
    тратит); permanent — повтор не исправит (доступ, адрес вебхука, удалённая запись)."""

    def __init__(self, message: str, method: str = "", code: str = "", description: str = "",
                 retry_at: datetime | None = None):
        super().__init__(message)
        self.method, self.code, self.description, self.retry_at = method, code, description, retry_at

    @property
    def not_found(self) -> bool:
        return self.code == "NOT_FOUND" or (not self.code and _text(self.description) in NOT_FOUND_TEXTS)

    @property
    def permanent(self) -> bool:
        return self.code in PERMANENT_CODES or self.not_found or (
            not self.code and _text(self.description) in ACCESS_DENIED_TEXTS)

    @property
    def deal_missing(self) -> bool:
        """Сделки в Битриксе нет: удалили после появления."""
        return self.not_found and self.method in DEAL_METHODS


def _reset_at(data: dict) -> datetime | None:
    """operating_reset_at из объекта time ответа (timestamp; api-reference/data-types.md#time)."""
    t = data.get("time")
    try:
        return datetime.fromtimestamp(float(t["operating_reset_at"]), timezone.utc)
    except (TypeError, KeyError, ValueError, OverflowError):
        return None


@dataclass
class Deal:
    id: int
    title: str
    stage_id: str
    stage_semantic: str   # P — в работе, S — успех, F — провал
    category_id: str
    created_at: datetime | None = None   # DATE_CREATE; нет или не разобрать — None


@dataclass
class Contact:
    id: int
    name: str
    phones: list[str]                    # нормализованные, без дублей


def _parse_date(value) -> datetime | None:
    """DATE_CREATE Битрикса: 2026-10-21T13:00:00+03:00. Без смещения — считаем UTC."""
    try:
        d = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def normalize_phone(raw: str) -> str | None:
    """+7 (999) 000-00-01, 89990000001 → +79990000001. Нечисловое → None."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits[0] == "9":
        digits = "7" + digits
    if len(digits) < 10:
        return None
    return "+" + digits


class Bitrix:
    """Всё, что робот делает в CRM. Чтобы подключить другую CRM, напишите класс с теми же
    методами get_deal / deal_contacts / deal_ids_after / max_deal_id / set_stage / comment
    (см. docs/РАЗРАБОТЧИКУ.md)."""

    def __init__(self, webhook: str, timeout: float = 30):
        self.webhook = webhook.rstrip("/") + "/"
        self.timeout = timeout
        # Ошибки лимитов: до срока клиент в Битрикс не ходит («уменьшить частоту запросов», limits.md).
        # "*" — весь портал (QUERY_LIMIT_EXCEEDED), имя метода — только он (OPERATION_TIME_LIMIT). → (срок, код)
        self._blocked: dict[str, tuple[datetime, str]] = {}
        self._limit_streak = 0                          # отказов по лимиту подряд: растёт задержка
        self._reset_at: dict[str, datetime] = {}        # operating_reset_at последнего успешного ответа метода

    async def _raw(self, method: str, params: dict | None = None) -> dict:
        now = datetime.now(timezone.utc)
        for key in ("*", method):
            until, code = self._blocked.get(key, (None, ""))
            if until is not None and now < until:
                raise BitrixError(f"{method}: {code} — вызовы Битрикса приостановлены до "
                                  f"{until.isoformat(timespec='seconds')}", method, code, retry_at=until)
        # Соединение на вызов: нагрузка — единицы запросов в минуту, а адрес может смениться в панели
        host = httpx.URL(self.webhook).host
        try:
            async with httpx.AsyncClient(base_url=self.webhook, timeout=self.timeout) as http:
                r = await http.post(f"{method}.json", json=params or {})
        except httpx.TransportError as e:
            raise BitrixError(f"{method}: нет связи с порталом {host}: {type(e).__name__}: {e} — проверьте адрес "
                              "портала в адресе вебхука", method) from e
        if r.is_redirect:
            # Адрес облачного портала сменили: Битрикс отвечает 301/302 с новым адресом (change-domen.md). По
            # редиректу не идём — POST превратился бы в GET без параметров; новый адрес вебхука вводит владелец
            moved = r.headers.get("location", "")
            new = moved.split("/rest/")[0] if "/rest/" in moved else moved
            raise BitrixError(f"{method}: портал {host} переадресует на {new or 'другой адрес'} — адрес портала "
                              "сменился? Возьмите новый адрес вебхука в Битриксе и сохраните в настройках", method)
        try:
            data = r.json()
        except ValueError:
            raise BitrixError(f"{method}: по адресу {host} ответил не Битрикс24 REST (HTTP {r.status_code}) — "
                              "проверьте адрес вебхука: https://портал/rest/<пользователь>/<код>/", method) from None
        if not isinstance(data, dict) or "result" not in data and "error" not in data:
            raise BitrixError(f"{method}: HTTP {r.status_code}, неожиданный ответ")
        reset = _reset_at(data)
        if "error" not in data:
            self._limit_streak = 0
            if reset is not None:
                self._reset_at[method] = reset
            return data
        code, description = str(data["error"] or ""), str(data.get("error_description") or "")
        retry_at = self._limit_wait(method, code, reset, now) if code in LIMIT_CODES else None
        detail = f" — повтор после {retry_at.isoformat(timespec='seconds')}" if retry_at else ""
        raise BitrixError(f"{method}: {code} {description}".rstrip() + detail, method, code, description, retry_at)

    def _limit_wait(self, method: str, code: str, reset: datetime | None, now: datetime) -> datetime:
        """Срок после ошибки лимита; запоминается, и до него клиент Битрикс не вызывает."""
        self._limit_streak += 1
        until = now + min(LIMIT_PAUSE * 2 ** (self._limit_streak - 1), LIMIT_PAUSE_MAX)
        if code == "OPERATION_TIME_LIMIT":
            # Срок сброса корзины — из этого ответа, если Битрикс его прислал, иначе из последнего успешного ответа
            # метода. Уже прошёл (ошибка повторилась после него) — ждём следующий сброс нарастающей задержкой
            known = next((t for t in (reset, self._reset_at.get(method)) if t is not None and t > now), None)
            if known is not None:
                until = known
        self._blocked["*" if code == "QUERY_LIMIT_EXCEEDED" else method] = (until, code)
        return until

    async def call(self, method: str, params: dict | None = None):
        return (await self._raw(method, params))["result"]

    async def call_all(self, method: str, params: dict) -> list:
        """Списочный метод целиком по смещению: Битрикс отдаёт по 50 строк и номер следующей страницы в next.
        Только для коротких справочников (этапы воронки): строка, удалённая между запросами страниц, сдвигает
        смещение. Новые сделки читает deal_ids_after — фильтром по ID."""
        rows: list = []
        start = 0
        while True:
            data = await self._raw(method, {**params, "start": start})
            rows += data["result"] or []
            nxt = data.get("next")
            if not nxt or int(nxt) <= start:
                return rows
            start = int(nxt)

    # ---------- для настройки ----------

    async def categories(self) -> list[tuple[str, str]]:
        """Воронки сделок: [(id, название)]. Основная — «0»."""
        try:
            res = await self.call("crm.category.list", {"entityTypeId": 2})
            items = [(str(c["id"]), c.get("name") or f"Воронка {c['id']}") for c in res.get("categories", [])]
        except BitrixError:
            items = []
        if not any(i == "0" for i, _ in items):
            items.insert(0, ("0", "Основная воронка"))
        return items

    async def stages(self, category_id: str | None) -> list[tuple[str, str]]:
        """Этапы воронки в работе: [(STATUS_ID, название)]. Закрывающие — SEMANTICS «S» (успешная) и «F»
        (провальная) — не входят (crm.status.list: «null — стадия в работе»; crm.status.fields: «"" — в работе»):
        этап «Не нашёл мессенджер» из них закрыл бы сделку, а закрытые сделки робот не трогает и после «Повторить»."""
        entity = "DEAL_STAGE" if category_id in (None, "", "0") else f"DEAL_STAGE_{category_id}"
        rows = await self.call_all("crm.status.list", {"filter": {"ENTITY_ID": entity}, "order": {"SORT": "ASC"}})
        return [(r["STATUS_ID"], r.get("NAME") or r["STATUS_ID"]) for r in rows if r.get("SEMANTICS") not in CLOSED]

    async def get_deal(self, deal_id: int) -> Deal:
        d = await self.call("crm.deal.get", {"id": deal_id})
        return Deal(
            id=int(d["ID"]),
            title=d.get("TITLE") or "",
            stage_id=d.get("STAGE_ID") or "",
            stage_semantic=d.get("STAGE_SEMANTIC_ID") or "P",
            category_id=str(d.get("CATEGORY_ID") or "0"),
            created_at=_parse_date(d.get("DATE_CREATE")) if d.get("DATE_CREATE") else None,
        )

    async def get_contact(self, contact_id: int) -> Contact:
        """Имя и телефоны контакта (нормализованные, без дублей)."""
        c = await self.call("crm.contact.get", {"id": contact_id})
        phones: list[str] = []
        for item in c.get("PHONE") or []:
            p = normalize_phone(item.get("VALUE", ""))
            if p and p not in phones:
                phones.append(p)
        return Contact(contact_id, (c.get("NAME") or "").strip(), phones)

    async def deal_contacts(self, deal_id: int) -> list[Contact]:
        """Все контакты сделки: главный (IS_PRIMARY) первым, остальные — по SORT привязки. Поле сделки CONTACT_ID
        в документации «Устаревшее», и какой из нескольких контактов в нём — не описано; список привязок —
        crm.deal.contact.items.get (scope crm, право «чтения» сделок). Компания сделки не читается. Контакт,
        удалённый между запросами («Not found»), пропускается: привязки к нему уже нет."""
        rows = await self.call("crm.deal.contact.items.get", {"id": deal_id}) or []
        order = sorted(enumerate(rows), key=lambda x: (x[1].get("IS_PRIMARY") != "Y", int(x[1].get("SORT") or 0),
                                                       x[0]))
        contacts: list[Contact] = []
        for _, r in order:
            try:
                contacts.append(await self.get_contact(int(r["CONTACT_ID"])))
            except BitrixError as e:
                if not e.not_found:
                    raise
        return contacts

    async def deal_ids_after(self, last_id: int, category_id: str | None) -> list[int]:
        """Сделки с ID больше last_id, по возрастанию. Страницы — фильтром по последнему полученному ID и start=-1
        (settings/performance/huge-data.md), а не смещением: удаление сделки или потеря прав на неё между запросами
        страниц сдвигает смещение, и следующая сделка выпала бы из выборки навсегда (курсор опроса уйдёт дальше неё).
        Повтор, пока страница полная (PAGE строк); total при start=-1 не считается."""
        ids: list[int] = []
        after = last_id
        while True:
            flt: dict = {">ID": after}
            if category_id is not None:
                flt["CATEGORY_ID"] = category_id
            rows = await self.call("crm.deal.list", {"filter": flt, "select": ["ID"], "order": {"ID": "ASC"},
                                                     "start": -1}) or []
            page = [int(r["ID"]) for r in rows]
            ids += page
            if len(page) < PAGE:
                return ids
            if max(page) <= after:
                # Полная страница без роста ID — фильтр >ID не применился; дальше цикл не кончился бы
                raise BitrixError(f"crm.deal.list: фильтр >ID не применён (страница после ID {after})")
            after = max(page)

    async def max_deal_id(self) -> int:
        rows = await self.call("crm.deal.list", {"select": ["ID"], "order": {"ID": "DESC"}, "start": -1})
        return int(rows[0]["ID"]) if rows else 0

    async def set_stage(self, deal_id: int, stage_id: str) -> None:
        await self.call("crm.deal.update", {"id": deal_id, "fields": {"STAGE_ID": stage_id}})

    async def comment(self, deal_id: int, text: str) -> None:
        """Комментарий в ленту сделки — менеджер видит, что сделал робот."""
        await self.call("crm.timeline.comment.add", {
            "fields": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal", "COMMENT": text},
        })
