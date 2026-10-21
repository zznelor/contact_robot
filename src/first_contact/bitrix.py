"""Битрикс24 REST через входящий вебхук."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx


# Размер страницы списочных методов: «всегда статичный — 50 записей» (list-methods-pecularities.md)
PAGE = 50


class BitrixError(RuntimeError):
    pass


@dataclass
class Deal:
    id: int
    title: str
    stage_id: str
    stage_semantic: str   # P — в работе, S — успех, F — провал
    category_id: str
    contact_id: int | None
    created_at: datetime | None = None   # DATE_CREATE; нет или не разобрать — None


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
    методами get_deal / get_contact / deal_ids_after / max_deal_id / set_stage / comment
    (см. docs/РАЗРАБОТЧИКУ.md)."""

    def __init__(self, webhook: str, timeout: float = 30):
        self.webhook = webhook.rstrip("/") + "/"
        self.timeout = timeout

    async def _raw(self, method: str, params: dict | None = None) -> dict:
        # Соединение на вызов: нагрузка — единицы запросов в минуту, а адрес может смениться в панели
        async with httpx.AsyncClient(base_url=self.webhook, timeout=self.timeout) as http:
            r = await http.post(f"{method}.json", json=params or {})
        try:
            data = r.json()
        except ValueError:
            raise BitrixError(f"{method}: HTTP {r.status_code}, ответ не JSON — проверьте адрес вебхука") from None
        if not isinstance(data, dict) or "result" not in data and "error" not in data:
            raise BitrixError(f"{method}: HTTP {r.status_code}, неожиданный ответ")
        if "error" in data:
            raise BitrixError(f"{method}: {data['error']} {data.get('error_description', '')}")
        return data

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
        """Этапы воронки: [(STATUS_ID, название)]."""
        entity = "DEAL_STAGE" if category_id in (None, "", "0") else f"DEAL_STAGE_{category_id}"
        rows = await self.call_all("crm.status.list", {"filter": {"ENTITY_ID": entity}, "order": {"SORT": "ASC"}})
        return [(r["STATUS_ID"], r.get("NAME") or r["STATUS_ID"]) for r in rows]

    async def get_deal(self, deal_id: int) -> Deal:
        d = await self.call("crm.deal.get", {"id": deal_id})
        return Deal(
            id=int(d["ID"]),
            title=d.get("TITLE") or "",
            stage_id=d.get("STAGE_ID") or "",
            stage_semantic=d.get("STAGE_SEMANTIC_ID") or "P",
            category_id=str(d.get("CATEGORY_ID") or "0"),
            contact_id=int(d["CONTACT_ID"]) if d.get("CONTACT_ID") else None,
            created_at=_parse_date(d.get("DATE_CREATE")) if d.get("DATE_CREATE") else None,
        )

    async def get_contact(self, contact_id: int) -> tuple[str, list[str]]:
        """Имя и телефоны контакта (нормализованные, без дублей)."""
        c = await self.call("crm.contact.get", {"id": contact_id})
        phones: list[str] = []
        for item in c.get("PHONE") or []:
            p = normalize_phone(item.get("VALUE", ""))
            if p and p not in phones:
                phones.append(p)
        return (c.get("NAME") or "").strip(), phones

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
