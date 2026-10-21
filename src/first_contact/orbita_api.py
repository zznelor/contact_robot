"""Орбита через публичный REST API v1 (ключ — заголовок X-Api-Key).

Спецификация: https://crm.my-orbita.ru/api/public/v1/openapi.json (копия в docs/).
Ключ выпускается в кабинете Орбиты → «Разработчикам» → «Ключи API»; роботу нужны права
dialogs:read, messages:send, dialogs:start.

Важное из спецификации:
- у каждой отправки обязателен Idempotency-Key: повтор с ним сообщение не шлёт, а
  возвращает исход первого вызова — поэтому обрыв связи здесь можно безопасно повторить;
- 201 — мессенджер подтвердил; 202 — сообщение могло уйти, исход придёт вебхуком
  message.sent / message.failed с тем же request_key;
- «человека нет в мессенджере» — 422 с кодом своего канала; «канал лежит» — другие коды.
"""

import hashlib
import hmac
from datetime import datetime, time, timedelta, timezone

import httpx

from .config import CHANNELS
from .orbita import FirstSendResult, Message, OrbitaError, ReplyResult, _parse_time

# Номера нет в этом мессенджере (или человек запретил писать ему) — пробуем следующий канал
NOT_FOUND_CODES = {"not_on_whatsapp", "max_not_found", "telegram_recipient_not_found",
                   "telegram_recipient_refused"}
# 422 уровня ключа или аккаунта — другой канал не поможет, повторять позже
KEY_LEVEL_CODES = {"idempotency_in_progress", "key_lacks_right", "subscription_inactive",
                   "daily_send_limit_exhausted", "executor_inactive", "idempotency_key_reused"}


# Суточные лимиты. daily_send_limit_exhausted — «1000 отправок в сутки UTC» (спецификация).
# telegram_search_limit_exhausted — «30 поисков в сутки»; когда сбрасывается, спецификация не говорит —
# ПРЕДПОЛОЖЕНИЕ: в полночь UTC, как суточный потолок ключа. Ошибка в нём безвредна: в полночь робот
# проверит снова и, если лимит ещё исчерпан, снова подождёт, не тратя попытку.
DAILY_LIMIT_CODES = {"daily_send_limit_exhausted", "telegram_search_limit_exhausted"}


def _next_utc_midnight() -> datetime:
    today = datetime.now(timezone.utc).date()
    return datetime.combine(today + timedelta(days=1), time(0, 0), tzinfo=timezone.utc)


def wait_until(code: str, retry_after_header, detail) -> datetime | None:
    """Срок из ответа Орбиты: заголовок Retry-After (429, idempotency_in_progress), detail.retry_after
    (telegram_flood_wait), суточный лимит — до полуночи UTC. Нет срока — None."""
    seconds = None
    for raw in (retry_after_header, detail.get("retry_after") if isinstance(detail, dict) else None):
        try:
            seconds = int(raw)
            break
        except (TypeError, ValueError):
            continue
    if seconds is not None and seconds > 0:
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)
    if code in DAILY_LIMIT_CODES:
        return _next_utc_midnight()
    return None


class OrbitaAPIError(OrbitaError):
    """Орбита ответила ошибкой. definite — Орбита точно ничего не отправила по этому запросу (4xx);
    5xx и idempotency_in_progress — исход неизвестен."""

    def __init__(self, status: int, code: str, detail, retry_at: datetime | None = None):
        super().__init__(f"HTTP {status} {code}: {detail}", retry_at)
        self.status, self.code, self.detail = status, code, detail

    @property
    def definite(self) -> bool:
        return self.status < 500 and self.code != "idempotency_in_progress"


class OrbitaAPI:
    def __init__(self, url: str, key: str, channels: list[str], timeout: float = 45):
        self.url = url.rstrip("/")
        self.key = key
        self.channel_order = channels
        self.timeout = timeout

    async def _request(self, method: str, path: str, *, json: dict | None = None,
                       params: dict | None = None, idempotency_key: str | None = None):
        headers = {"X-Api-Key": self.key}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as http:
                r = await http.request(method, self.url + path, json=json, params=params, headers=headers)
        except httpx.HTTPError as e:
            # Отправки идемпотентны: повтор с тем же ключом не задублирует сообщение,
            # поэтому обрыв — обычная повторяемая ошибка, а не «исход неизвестен».
            raise OrbitaError(f"{method} {path}: {type(e).__name__}: {e}") from e
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = None
            detail = body.get("detail") if isinstance(body, dict) else r.text[:300]
            code = detail.get("code", "") if isinstance(detail, dict) else ""
            raise OrbitaAPIError(r.status_code, code, detail, wait_until(code, r.headers.get("retry-after"), detail))
        return r.status_code, (r.json() if r.content else {})

    # ---------- проверка ----------

    async def account(self) -> dict:
        return (await self._request("GET", "/account"))[1]

    async def channels(self) -> list[dict]:
        """Каналы аккаунта: канал / подключён / сотрудник."""
        items = (await self._request("GET", "/channels"))[1].get("items", [])
        return [{"канал": c.get("provider"), "подключён": bool(c.get("is_connected")),
                 "сотрудник": c.get("employee_id")} for c in items]

    async def channel_status(self) -> dict[str, bool | None]:
        """Все мессенджеры робота, даже не заведённые в Орбите: True — подключён хоть у одного
        сотрудника, False — подключение есть, но отвалилось, None — не подключали вовсе."""
        status: dict[str, bool | None] = dict.fromkeys(CHANNELS)
        for c in await self.channels():
            status[c["канал"]] = bool(status.get(c["канал"])) or c["подключён"]
        return status

    # ---------- отправка ----------

    async def connected_channels(self) -> set[str]:
        return {c["канал"] for c in await self.channels() if c["подключён"]}

    async def start_dialog(self, channel: str, phone: str, text: str, idempotency_key: str,
                           name: str | None = None) -> FirstSendResult:
        """«Написать первым» в один канал. Ключ передаётся как есть (в нём уже канал).

        sent (201) / pending (202) — сообщение ушло или могло уйти; not_found — номера нет в этом
        мессенджере; unchecked — канал не смог проверить номер (raw — код отказа); invalid_phone — 400,
        Орбита не приняла номер. Отказ уровня ключа или аккаунта, сбой связи, 5xx — исключение."""
        body = {"channel": channel, "phone": phone, "text": text}
        if name:
            body["name"] = name
        try:
            status, data = await self._request("POST", "/dialogs", json=body, idempotency_key=idempotency_key)
        except OrbitaAPIError as e:
            if e.code in NOT_FOUND_CODES:
                return FirstSendResult("not_found", channel=channel, raw=e.code)
            if e.status == 400:
                # Адресат у нас ровно один, текст не пустой — по спецификации 400 здесь значит невалидный номер
                return FirstSendResult("invalid_phone", channel=channel, raw=e.code)
            if e.status == 422 and e.code not in KEY_LEVEL_CODES:
                return FirstSendResult("unchecked", channel=channel, raw=e.code, retry_at=e.retry_at)
            raise
        if status == 202:
            return FirstSendResult("pending", None, channel, data, request_key=data.get("request_key"))
        dialog = (data.get("dialog") or {}).get("id")
        return FirstSendResult("sent", str(dialog) if dialog is not None else None, channel, data)

    async def send_first(self, phone: str, text: str, idempotency_key: str,
                         name: str | None = None) -> FirstSendResult:
        """Все выбранные каналы по порядку (тестовая отправка из панели). Робот перебирает каналы сам
        (Worker.first_contact), чтобы сохранять намерение отправки до запроса."""
        connected = await self.connected_channels()
        if not connected & set(self.channel_order):
            # Не «мессенджер не найден»: иначе живой клиент уйдёт на этап NO_MESSENGER
            raise OrbitaError(f"нет подключённых каналов из {self.channel_order}")
        not_found, unchecked = [], []
        for ch in self.channel_order:
            if ch not in connected:
                unchecked.append(f"{ch}: не подключён")
                continue
            res = await self.start_dialog(ch, phone, text, f"{idempotency_key}-{ch}", name)
            if res.status in ("sent", "pending"):
                return res
            if res.status == "invalid_phone":
                raise OrbitaError(f"Орбита не приняла номер {phone} ({res.raw})")
            if res.status == "not_found":
                not_found.append(ch)
            else:
                unchecked.append(f"{ch}: {res.raw}")
        if unchecked:
            # Хотя бы один выбранный канал не проверил номер — не утверждаем, что мессенджера нет
            raise OrbitaError(f"не найден в {not_found or '—'}, не проверен: {'; '.join(unchecked)}")
        return FirstSendResult("no_messenger", raw={"проверены каналы": not_found})

    async def send_reply(self, dialog_id: str, text: str, idempotency_key: str) -> ReplyResult:
        """201 — мессенджер подтвердил; 202 — команда передана (для WhatsApp/Max обычный ответ), исход придёт
        вебхуком message.sent / message.failed с request_key."""
        status, data = await self._request("POST", f"/dialogs/{dialog_id}/messages", json={"text": text},
                                           idempotency_key=idempotency_key)
        if status == 202:
            return ReplyResult("pending", data.get("request_key"))
        return ReplyResult("sent")

    # ---------- история ----------

    async def history(self, dialog_id: str) -> list[Message]:
        """Метаданные сообщений (без текста): направление и время — этого хватает,
        чтобы понять, ответил ли клиент. Право dialogs:read, без лимита 10/мин у history."""
        data = (await self._request("GET", f"/dialogs/{dialog_id}/messages", params={"limit": 200}))[1]
        return [
            Message(author=str(m.get("direction") or ""), text="",
                    at=_parse_time(m.get("sent_at")),
                    outgoing={"outgoing": True, "incoming": False}.get(m.get("direction")))
            for m in data.get("items", [])
        ]


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str,
                     max_skew: int = 300) -> bool:
    """X-Orbita-Signature: v1=<hex HMAC-SHA256 секретом подписки над "<timestamp>.<тело>">."""
    if not (secret and timestamp and signature):
        return False
    try:
        if max_skew and abs(datetime.now().timestamp() - int(timestamp)) > max_skew:
            return False
    except ValueError:
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(f"v1={expected}", s.strip()) for s in signature.split(","))
