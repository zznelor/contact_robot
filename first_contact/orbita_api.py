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
from datetime import datetime

import httpx

from .orbita import FirstSendResult, Message, OrbitaError, _parse_time

# Номера нет в этом мессенджере (или человек запретил писать ему) — пробуем следующий канал
NOT_FOUND_CODES = {"not_on_whatsapp", "max_not_found", "telegram_recipient_not_found",
                   "telegram_recipient_refused"}


class OrbitaAPIError(OrbitaError):
    def __init__(self, status: int, code: str, detail):
        super().__init__(f"HTTP {status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


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
            raise OrbitaAPIError(r.status_code, code, detail)
        return r.status_code, (r.json() if r.content else {})

    # ---------- проверка ----------

    async def account(self) -> dict:
        return (await self._request("GET", "/account"))[1]

    async def channels(self) -> list[dict]:
        """Каналы аккаунта: канал / подключён / сотрудник."""
        items = (await self._request("GET", "/channels"))[1].get("items", [])
        return [{"канал": c.get("provider"), "подключён": bool(c.get("is_connected")),
                 "сотрудник": c.get("employee_id")} for c in items]

    # ---------- отправка ----------

    async def send_first(self, phone: str, text: str, idempotency_key: str,
                         name: str | None = None) -> FirstSendResult:
        connected = {c["канал"] for c in await self.channels() if c["подключён"]}
        order = [ch for ch in self.channel_order if ch in connected]
        if not order:
            # Не «мессенджер не найден»: иначе живой клиент уйдёт на этап NO_MESSENGER
            raise OrbitaError(f"нет подключённых каналов из {self.channel_order}")

        not_found, broken = [], []
        for ch in order:
            body = {"channel": ch, "phone": phone, "text": text}
            if name:
                body["name"] = name
            try:
                status, data = await self._request("POST", "/dialogs", json=body,
                                                   idempotency_key=f"{idempotency_key}-{ch}")
            except OrbitaAPIError as e:
                if e.code in NOT_FOUND_CODES:
                    not_found.append(ch)
                    continue
                if e.status == 422 and e.code not in ("idempotency_in_progress", "key_lacks_right",
                                                      "subscription_inactive", "daily_send_limit_exhausted",
                                                      "executor_inactive", "idempotency_key_reused"):
                    broken.append(f"{ch}: {e.code}")   # этот канал не смог проверить номер — пробуем другой
                    continue
                raise                                   # отказ уровня ключа/аккаунта — повторять позже
            if status == 202:
                return FirstSendResult("pending", None, ch, data, request_key=data.get("request_key"))
            dialog = (data.get("dialog") or {}).get("id")
            return FirstSendResult("sent", str(dialog) if dialog is not None else None, ch, data)

        if broken:
            # Хотя бы один канал не смог проверить номер — не утверждаем, что мессенджера нет
            raise OrbitaError(f"не найден в {not_found or '—'}, не проверен: {'; '.join(broken)}")
        return FirstSendResult("no_messenger", raw={"проверены каналы": not_found})

    async def send_reply(self, dialog_id: str, text: str, idempotency_key: str) -> dict:
        # 201 — подтверждено; 202 — для WhatsApp/Max обычный ответ («команда передана»)
        return (await self._request("POST", f"/dialogs/{dialog_id}/messages", json={"text": text},
                                    idempotency_key=idempotency_key))[1]

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
