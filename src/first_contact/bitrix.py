"""Битрикс24 REST через входящий вебхук."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx


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

    async def call(self, method: str, params: dict | None = None):
        # Соединение на вызов: нагрузка — единицы запросов в минуту, а адрес может смениться в панели
        async with httpx.AsyncClient(base_url=self.webhook, timeout=self.timeout) as http:
            r = await http.post(f"{method}.json", json=params or {})
        try:
            data = r.json()
        except ValueError:
            raise BitrixError(f"{method}: HTTP {r.status_code}, ответ не JSON — проверьте адрес вебхука") from None
        if "error" in data:
            raise BitrixError(f"{method}: {data['error']} {data.get('error_description', '')}")
        return data["result"]

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
        rows = await self.call("crm.status.list", {"filter": {"ENTITY_ID": entity}, "order": {"SORT": "ASC"}})
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
        flt: dict = {">ID": last_id}
        if category_id is not None:
            flt["CATEGORY_ID"] = category_id
        rows = await self.call("crm.deal.list", {
            "filter": flt, "select": ["ID"], "order": {"ID": "ASC"},
        })
        return [int(r["ID"]) for r in rows]

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
