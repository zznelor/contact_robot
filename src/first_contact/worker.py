"""Логика первого контакта.

1. Новая сделка → первое сообщение клиенту в мессенджер через Орбиту.
2. Мессенджер не найден → этап сделки «Не нашёл мессенджер».
3. Клиент молчит N часов → напоминание (если включено).
4. Клиент ответил → один ответ по шаблону (если включён), дальше работает менеджер.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config
from .bitrix import Bitrix
from .config import Env, Settings
from .db import DB, now
from .orbita import (CLOCK_SKEW, FirstSendResult, OrbitaError, ReplyResult, _parse_time, answered_after,
                     client_replied, first_incoming)
from .orbita_api import OrbitaAPI, OrbitaAPIError

log = logging.getLogger("first_contact")

MAX_ATTEMPTS = 5
# Пауза перед следующей попыткой после сбоя шага: 2, 4, 8, 16 мин — пятая (последняя) примерно через полчаса
RETRY_PAUSE = timedelta(minutes=2)
# Сделки, которые ждали дольше (робот был выключен или лежала Орбита), не трогаем:
# писать «вы оставили заявку» через неделю — плохо.
MAX_AGE = timedelta(hours=48)
# У контактов новой сделки нет телефона — ждём столько от её появления: контакт могут привязать позже
PHONE_WAIT = timedelta(minutes=10)
# Орбита ответила 202 на «написать первым», а вебхука с исходом нет так долго — зовём человека
PENDING_TIMEOUT = timedelta(minutes=30)
# Ответ по шаблону имеет смысл, только пока клиент ещё в разговоре
REPLY_MAX_LATE = timedelta(minutes=30)
# Без вебхуков Орбиты ответ клиента ищем опросом истории — не чаще, чем раз в столько
REPLY_POLL = timedelta(minutes=5)
REPLY_POLL_DAYS = timedelta(days=3)
# Обработанные вебхуки Орбиты хранятся столько: дубли доставки и сверка ответа клиента до подтверждения
# первого сообщения смотрят только недавние события
EVENTS_KEEP = timedelta(days=30)


# Какие статусы можно вернуть в работу кнопкой «Повторить»
RETRYABLE = {"error", "needs_check", "skipped", "no_messenger", "no_phone"}


class UnknownOutcome(Exception):
    """Отправка могла уйти, а могла и нет. Повторять нельзя — нужен человек. fields — что записать той же записью,
    что ставит «проверить вручную»."""

    def __init__(self, message: str, **fields):
        super().__init__(message)
        self.fields = fields


@dataclass
class _AfterOutcome:
    """Внешние действия после применённого исхода отправки — уже после транзакции с записью исхода: комментарии в
    сделку (по возможности) и возврат этапа (сохранённая работа, его доделает шаг stages)."""
    deal_id: int
    notes: list[str] = field(default_factory=list)
    restore_stage: bool = False


def _ts(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso) if iso else None


class Worker:
    def __init__(self, env: Env, s: Settings, db: DB, bitrix=None, orbita=None):
        self.env, self.s, self.db = env, s, db
        self.bitrix, self.orbita = bitrix, orbita
        # lock — состояние сделок и флаг «включён»: каждый шаг по одной сделке, ручные действия панели,
        # события Орбиты и включение/выключение идут под ним по очереди. Шаг держит замок от проверки
        # статуса до записи результата, поэтому ручное действие ждёт текущий шаг (секунды), а не весь проход.
        # _pass — проходы целиком (фоновый цикл и кнопки панели) не идут одновременно.
        self.lock = asyncio.Lock()
        self._pass = asyncio.Lock()
        self._inbox = asyncio.Lock()     # события Орбиты из базы разбираются по одному разу
        if bitrix is None and orbita is None:
            self.reconnect()
        if s.enabled and db.get("start_deal_id") is None and db.get("last_deal_id") is not None:
            # Установка, включённая до появления границы: исходной границы не знаем, берём курсор опроса
            db.put("start_deal_id", db.get("last_deal_id"))

    def reconnect(self) -> None:
        """Пересобрать клиенты после смены настроек в панели."""
        s = self.s
        self.bitrix = Bitrix(s.bitrix_webhook) if s.bitrix_webhook else None
        self.orbita = OrbitaAPI(s.orbita_api_url, s.orbita_api_key, s.orbita_channels) if s.orbita_api_key else None

    def save(self, **changes) -> None:
        config.save(self.db, self.s, **changes)
        if changes.keys() & {"bitrix_webhook", "orbita_api_key", "orbita_api_url", "orbita_channels"}:
            self.reconnect()

    @property
    def dry_run(self) -> bool:
        return self.env.dry_run

    # ---------- общее ----------

    def in_window(self) -> bool:
        t = datetime.now(self.s.tz).time()
        start, end = self.s.send_window
        return start <= t < end

    def render(self, key: str, name: str, deal_title: str = "") -> str:
        tpl = {"first": self.s.tpl_first, "followup": self.s.tpl_followup, "reply": self.s.tpl_reply}[key]
        return render(tpl, name or self.s.tpl_no_name, deal_title)

    async def note(self, deal_id: int, text: str) -> None:
        """Комментарий в ленту сделки. Ошибка комментария не должна ломать основное действие."""
        if self.dry_run or self.bitrix is None:
            return
        try:
            await self.bitrix.comment(deal_id, f"[Робот первого контакта] {text}")
        except Exception as e:  # noqa: BLE001
            log.warning("сделка %s: не удалось оставить комментарий: %s", deal_id, e)

    def event(self, deal_id: int | None, name: str, detail: str = "") -> None:
        log.info("сделка %s: %s %s", deal_id, name, detail)
        self.db.log(deal_id, name, detail)

    @property
    def active(self) -> bool:
        return self.s.enabled and self.s.configured

    async def enable(self) -> int:
        """Включить робота. Сделки, созданные до включения, не трогаем."""
        async with self.lock:
            start = await self.bitrix.max_deal_id()
            self.db.put("last_deal_id", start)      # курсор опроса: растёт
            self.db.put("start_deal_id", start)     # граница включения: неизменна до следующего включения
            self.save(enabled=True)
            self.event(None, "робот включён", f"сделки до ID {start} включительно не трогаем")
            return start

    async def disable(self) -> None:
        """Выключить. Ждёт шаг, который уже идёт по одной сделке; после возврата робот ничего не отправит."""
        async with self.lock:
            self.save(enabled=False)
            self.event(None, "робот выключен")

    # ---------- цикл ----------

    # Шаги прохода — по порядку; в панели их можно запустить по одному
    STEPS = {
        "events": "Обработать принятые события Орбиты",
        "cleanup": "Удалить обработанные события и исходы отправок Орбиты старше 30 дней",
        "poll": "Забрать новые сделки из Битрикса",
        "first": "Написать первым по новым сделкам",
        "followup": "Напоминания и итог по тем, кто молчит",
        "pending": "Проверить неподтверждённые отправки",
        "stages": "Вернуть этап сделкам, которым написали после «Повторить»",
        "replies": "Проверить ответы клиентов в истории",
        "auto_reply": "Отправить ответы по шаблону",
    }

    async def tick(self) -> None:
        async with self._pass:
            # Принятые события Орбиты разбираются (и старые обработанные чистятся) и при выключенном роботе
            await self._isolated("events")
            await self._isolated("cleanup")
            if not self.active:
                return
            for step in self.STEPS:
                if step not in ("events", "cleanup"):
                    await self._isolated(step)

    async def _isolated(self, step: str) -> None:
        """Сбой одного шага (например, Битрикс недоступен для опроса) не отменяет остальные шаги прохода:
        ответы по шаблону, таймауты, напоминания по уже сохранённым сделкам Битрикс-опроса не ждут."""
        try:
            await self._step(step, manual=False)
        except Exception:  # noqa: BLE001
            log.exception("проход: шаг %s (%s) не выполнен", step, self.STEPS[step])

    async def run_step(self, step: str) -> None:
        """Один шаг прохода по кнопке из панели."""
        if step not in self.STEPS:
            raise KeyError(step)
        if not self.active:
            return
        async with self._pass:
            await self._step(step, manual=True)

    async def _step(self, step: str, manual: bool) -> None:
        # Замок берётся на каждую сделку (в _guarded и ниже), а не на весь проход: между сделками
        # проходят ручные действия и события Орбиты, а выключение останавливает оставшуюся очередь.
        if step == "events":
            await self.process_inbox()
        elif step == "cleanup":
            before = (datetime.now(timezone.utc) - EVENTS_KEEP).isoformat(timespec="seconds")
            if n := self.db.purge_events(before):
                log.info("удалено обработанных событий Орбиты старше %s дней: %s", EVENTS_KEEP.days, n)
            if n := self.db.purge_outcomes(before):
                log.info("удалено исходов отправок Орбиты старше %s дней: %s", EVENTS_KEEP.days, n)
        elif step == "poll":
            async with self.lock:
                if self.active:
                    await self.poll_new_deals()
        elif step == "first":
            for row in self.db.by_status("new"):
                await self._guarded(row["deal_id"], self.first_contact)
        elif step == "followup":
            for row in self.db.by_status("sent_first"):
                await self._guarded(row["deal_id"], self.followup)
            for row in self.db.by_status("sent_followup"):
                await self._guarded(row["deal_id"], self.after_followup)
        elif step == "pending":
            for row in self.db.by_status("pending_first"):
                await self._guarded(row["deal_id"], self.pending_timeout)
            for row in self.db.unconfirmed_sends():
                await self._guarded(row["deal_id"], self.send_timeout)
        elif step == "stages":
            for row in self.db.stages_to_restore():
                await self._guarded(row["deal_id"], self._restore_stage)
        elif step == "replies":
            # Сам — только без вебхуков и при ответе по шаблону; по кнопке — всегда и без паузы REPLY_POLL
            if manual or (self.s.reply_on and not self.s.orbita_webhook_secret):
                await self.poll_replies(force=manual)
        elif step == "auto_reply":
            for row in self.db.replies_due(now()):
                await self._guarded(row["deal_id"], self.auto_reply)

    async def on_deal_added(self, deal_id: int) -> None:
        """Событие ONCRMDEALADD из Битрикса: обработать сразу, не дожидаясь опроса."""
        async with self.lock:
            if not self.active:
                return
            start = self.db.get("start_deal_id")
            if self.db.deal(deal_id) is None and start is not None and deal_id <= int(start):
                # Запоздавшее событие о сделке, созданной до включения: её робот не трогает
                # (взять такую можно вручную — «Взять в работу»)
                log.info("сделка %s: событие о сделке до включения (граница %s) пропущено", deal_id, start)
                return
            if self.db.add_deal(deal_id):
                self.event(deal_id, "новая сделка (событие)")
            await self._attempt(deal_id, self.first_contact)

    async def poll_new_deals(self) -> None:
        """Подстраховка к событиям: события Битрикса теряются, опрос — нет."""
        last = self.db.get("last_deal_id")
        if last is None:
            start = await self.bitrix.max_deal_id()
            self.db.put("last_deal_id", start)
            self.db.put("start_deal_id", start)
            self.event(None, "старт", f"сделки до ID {start} включительно не трогаем")
            return
        for deal_id in await self.bitrix.deal_ids_after(int(last), self.s.category):
            if self.db.add_deal(deal_id):
                self.event(deal_id, "новая сделка (опрос)")
            self.db.put("last_deal_id", deal_id)

    async def _guarded(self, deal_id: int, step) -> None:
        """Шаг по одной сделке под замком; робот выключили, пока шаг ждал очереди, — ничего не делаем."""
        async with self.lock:
            if self.active:
                await self._attempt(deal_id, step)

    async def _attempt(self, deal_id: int, step) -> None:
        """Шаг с учётом ошибок. Вызывается под self.lock."""
        before = self.db.deal(deal_id)
        if before and before["next_attempt_at"] and datetime.now(timezone.utc) < _ts(before["next_attempt_at"]):
            return                                      # пауза после сбоя или срок, названный Орбитой
        try:
            await step(deal_id)
        except UnknownOutcome as e:
            self.db.update(deal_id, status="needs_check", last_error=str(e), **e.fields)
            self.event(deal_id, "исход отправки неизвестен", str(e))
            await self.note(deal_id, "Не уверен, ушло ли сообщение клиенту. Проверьте переписку в Орбите.")
        except Exception as e:  # noqa: BLE001
            retry_at = getattr(e, "retry_at", None)
            if retry_at is not None:
                # Орбита назвала срок (Retry-After, retry_after, суточный лимит) — ждём его, попытку не тратим
                self.db.update(deal_id, next_attempt_at=retry_at.isoformat(timespec="seconds"),
                               last_error=f"{type(e).__name__}: {e}")
                self.event(deal_id, "отложено", f"до {retry_at.isoformat(timespec='seconds')}: {e}")
                return
            row = self.db.deal(deal_id)
            attempts = (row["attempts"] if row else 0) + 1
            if getattr(e, "deal_missing", False):
                # Сделку удалили в Битриксе: работы нет, комментарий оставить некуда
                self.db.update(deal_id, status="skipped", attempts=attempts, last_error=f"сделки нет в Битриксе: {e}")
                self.event(deal_id, "пропущена", f"сделки нет в Битриксе (удалена?): {e}")
                return
            if getattr(e, "permanent", False):
                # Повтор не исправит (нет прав у пользователя вебхука, вебхук удалён, нет scope): сразу к человеку,
                # не через пять пауз. «Повторить» вернёт сделку, когда права поправят
                self.db.update(deal_id, attempts=attempts, status="error", next_attempt_at=None,
                               last_error=f"{type(e).__name__}: {e} — повтор не поможет")
                self.event(deal_id, "ошибка", f"повтор не поможет: {e}")
                log.error("сделка %s: ошибка, которую повтор не исправит: %s: %s", deal_id, type(e).__name__, e)
                await self.note(deal_id, f"Не смог обработать заявку: {e}. Повтор не поможет — проверьте права "
                                         "пользователя вебхука в Битриксе, затем «Повторить» в панели робота.")
                return
            status = "error" if attempts >= MAX_ATTEMPTS else (row["status"] if row else "new")
            pause = datetime.now(timezone.utc) + RETRY_PAUSE * 2 ** (attempts - 1)
            self.db.update(deal_id, attempts=attempts, last_error=f"{type(e).__name__}: {e}", status=status,
                           next_attempt_at=None if status == "error" else pause.isoformat(timespec="seconds"))
            self.event(deal_id, "ошибка", f"попытка {attempts}: {e}")
            if status == "error":
                log.error("сделка %s: ошибка после %s попыток: %s: %s", deal_id, attempts, type(e).__name__, e)
                await self.note(deal_id, f"Не смог обработать заявку после {attempts} попыток: {e}")
        # Сделка продвинулась — прошлые сбои не копятся до «ошибки» на следующем шаге: сброс делает сама запись
        # нового статуса (DB.update), в том числе вне _attempt (вебхуки, опрос ответов)

    async def retry(self, deal_id: int, who: str) -> str:
        """«Повторить» из панели: вернуть сделку туда, где робот остановился. Возвращает новый статус.

        Первое сообщение не ушло — снова в очередь, с новым ключом идемпотентности (иначе Орбита
        вернёт прежний исход) и без проверки этапа «Не нашёл мессенджер» (его поставил сам робот).
        Ушло — ждём напоминание или итог; клиент ответил — ответ по шаблону, если он ещё нужен.
        """
        async with self.lock:
            return self._retry(deal_id, who)

    def _retry(self, deal_id: int, who: str) -> str:
        row = self.db.deal(deal_id)
        if row is None or row["status"] not in RETRYABLE:
            raise ValueError("эту сделку сейчас нельзя повторить")
        # Напоминание или ответ по шаблону передан (202), а подтверждения нет: шаг не считается сделанным,
        # повтор идёт тем же ключом (Орбита вернёт исход прошлого запроса, второе сообщение не уйдёт)
        followup_unconfirmed = bool(row["followup_sent_at"] and row["followup_request_key"])
        reply_unconfirmed = bool(row["reply_sent_at"] and row["reply_request_key"])
        if row["client_replied_at"]:
            status = "replied"
        elif row["followup_sent_at"] and not followup_unconfirmed:
            status = "sent_followup"
        elif row["first_sent_at"]:
            status = "sent_first"
        else:
            status = "new"
        # Решение человека — продолжить: ручная остановка снимается
        # Человек проверил переписку (панель предупреждала о втором сообщении): отметка об отвергнутом ключе снимается
        fields: dict = {"status": status, "attempts": 0, "last_error": None, "next_attempt_at": None,
                        "manual_stop_at": None, "reused_key_step": None}
        # Повтор шага после определённого отказа — новый ключ идемпотентности этого шага (-r<n>): с прежним
        # Орбита вернула бы исход прошлой попытки и сообщение не ушло бы. Исход прошлой попытки неизвестен
        # (сохранён followup_key / reply_key) — тот же ключ: если сообщение дошло, Орбита второе не отправит.
        # request_key прежней отправки (202) остаётся, пока повтор не ушёл: её исход, пришедший в этот промежуток,
        # применяется (_send_outcome), а не теряется
        if status == "sent_first":
            if followup_unconfirmed:
                fields.update(followup_sent_at=None)
            elif not row["followup_key"]:
                fields["followup_retry_n"] = (row["followup_retry_n"] or 0) + 1
        if status == "replied" and self.s.reply_on and \
                (reply_unconfirmed or (not row["reply_sent_at"] and row["reply_due_at"])):
            # Ручной повтор продлевает срок: ответ уходит, даже если с сообщения клиента прошло больше 30 мин
            fields.update(reply_due_at=now(), reply_retried_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          reply_sent_at=None)
            if not row["reply_key"] and not reply_unconfirmed:
                fields["reply_retry_n"] = (row["reply_retry_n"] or 0) + 1
        if status == "new":
            # Новая попытка — с новым ключом (-r<n>). request_key и pending_at прежней отправки (202) остаются, пока
            # новая не передана: её исход, пришедший в этот промежуток, применяется (_apply_first), а не теряется
            fields.update(retry_n=(row["retry_n"] or 0) + 1, retried_at=now())
            if row["status"] == "needs_check":
                # Человек проверил переписку (панель предупреждает о втором сообщении) — неизвестный исход
                # прошлой отправки больше не выясняем
                fields.update(intent_phone=None, intent_channel=None, intent_key=None)
        self.db.update(deal_id, **fields)
        self.event(deal_id, "вручную: повторить", f"{who} → {status}")
        return status

    async def stop(self, deal_id: int, who: str) -> bool:
        """«Остановить робота» по сделке. False — сделки нет в базе.

        Ждёт шаг по сделке, который уже идёт; после возврата робот по ней ничего не отправит."""
        async with self.lock:
            row = self.db.deal(deal_id)
            if row is None:
                return False
            if row["status"] == "replied":
                self.db.update(deal_id, reply_due_at=None, manual_stop_at=now())
            else:
                self.db.update(deal_id, status="skipped", last_error="остановлено вручную", reply_due_at=None,
                               manual_stop_at=now())
            self.event(deal_id, "вручную: остановить", who)
            return True

    async def mark_replied(self, deal_id: int, who: str) -> bool:
        """«Клиент ответил» из панели: робот больше не пишет, ответ по шаблону не отправляется."""
        async with self.lock:
            row = self.db.deal(deal_id)
            if row is None:
                return False
            self.db.update(deal_id, status="replied", reply_due_at=None, manual_stop_at=now(),
                           client_replied_at=row["client_replied_at"] or now())
            self.event(deal_id, "вручную: клиент ответил", who)
            return True

    async def take(self, deal_id: int, who: str) -> None:
        """«Взять в работу» из панели: сделка, которой нет в базе робота, — как новая."""
        async with self.lock:
            if not self.active:
                return
            self.db.add_deal(deal_id)
            # Ручной путь: граница включения не действует, 48 ч считаются от этого момента
            self.db.update(deal_id, retried_at=now())
            self.event(deal_id, "вручную: взять в работу", who)
            await self._attempt(deal_id, self.first_contact)

    async def pending_timeout(self, deal_id: int) -> None:
        """202 на «написать первым», а исхода так и нет — зовём человека."""
        row = self.db.deal(deal_id)
        if row is None or row["status"] != "pending_first":
            return
        if datetime.now(timezone.utc) - _ts(row["pending_at"]) <= PENDING_TIMEOUT:
            return
        self.db.update(deal_id, status="needs_check", last_error="нет подтверждения отправки от Орбиты")
        self.event(deal_id, "нет подтверждения отправки", row["request_key"] or "")
        await self.note(deal_id, "Орбита не подтвердила, что первое сообщение ушло. Проверьте переписку.")

    async def send_timeout(self, deal_id: int) -> None:
        """202 на напоминание или ответ по шаблону, а исхода так и нет — зовём человека, как у первого сообщения.
        Ключи остаются: поздний вебхук ещё применится, «Повторить» пойдёт тем же ключом, новой отправки нет."""
        row = self.db.deal(deal_id)
        if row is None or row["manual_stop_at"]:
            return                                      # оператор уже решил: робот по сделке больше не работает
        for status, column, at, title, text in (
                ("sent_followup", "followup_request_key", "followup_sent_at", "напоминание", "напоминание ушло"),
                ("replied", "reply_request_key", "reply_sent_at", "ответ по шаблону", "ответ по шаблону ушёл")):
            if row["status"] != status or not row[column] or not row[at]:
                continue
            if datetime.now(timezone.utc) - _ts(row[at]) <= PENDING_TIMEOUT:
                return
            self.db.update(deal_id, status="needs_check", last_error=f"нет подтверждения от Орбиты: {title}")
            self.event(deal_id, f"{title}: нет подтверждения отправки", row[column])
            await self.note(deal_id, f"Орбита не подтвердила, что {text}. Проверьте переписку.")
            return

    # ---------- первое сообщение и «нет мессенджера» ----------

    async def first_contact(self, deal_id: int) -> None:
        row = self.db.deal(deal_id)
        if row is None or row["status"] != "new":
            return
        connected: set[str] | None = None
        if row["intent_key"] and not self.dry_run:
            # Прошлая попытка оборвалась с неизвестным исходом: сообщение могло уйти, а его исход (message.sent /
            # message.failed) мог уже прийти и ждать ключа. Исход выясняется ДО решений по свежим данным Битрикса
            # (контакты, телефоны, этап, воронка): это не новая отправка, а завершение переданной, и отвязанный
            # контакт или закрытая сделка не отменяют того, что клиент мог уже получить сообщение
            if not self.in_window():
                return                                  # повтор может оказаться и первой отправкой — только в окно
            done, connected = await self._resume_first(deal_id, row)
            if done:
                return
            row = self.db.deal(deal_id)                 # исход определён и это не отправка — намерение снято
        # 48 ч — от создания сделки в Битриксе (DATE_CREATE; пока не спросили Битрикс — от момента, когда
        # робот её увидел, это не раньше) или от ручного повтора / «Взять в работу», что позже
        if self._too_old(deal_id, row, _ts(row["created_at"])):
            return
        if not self.in_window():
            return

        deal = await self.bitrix.get_deal(deal_id)
        if deal.created_at and self._too_old(deal_id, row, deal.created_at):
            return
        if deal.category_id != self.s.category:
            self.db.update(deal_id, status="skipped", last_error=f"другая воронка ({deal.category_id})")
            self.event(deal_id, "пропущена", f"другая воронка ({deal.category_id})")
            return
        # Человек нажал «Повторить» или этап «Не нашёл мессенджер» поставил сам робот (запомнен прежний этап,
        # а статус записать не успел) — этот этап не помеха
        manual = bool(row["retry_n"]) or bool(row["stage_before"])
        if deal.stage_semantic != "P" or (deal.stage_id == self.s.no_messenger_stage and not manual):
            self.db.update(deal_id, status="skipped", last_error=f"этап {deal.stage_id}")
            self.event(deal_id, "пропущена", f"этап {deal.stage_id}")
            return

        # Телефоны всех контактов сделки: главный контакт первым, затем по порядку, без дублей. Имя — того контакта,
        # у которого номер встретился первым: с ним уходит сообщение на этот номер
        names: dict[str, str] = {}
        for contact in await self.bitrix.deal_contacts(deal_id):
            for p in contact.phones:
                names.setdefault(p, contact.name)
        phones = list(names)
        if not phones:
            # Источник заявки может создать сделку, а контакт привязать следующим вызовом (ONCRMDEALADD приходит
            # при создании): сделка ждёт в очереди и перечитывается на проходах, пока с её появления не прошло
            # PHONE_WAIT. Отсчёт — от DATE_CREATE (нет — от момента, когда робот её увидел), поэтому поздно
            # увиденная сделка и ручной «Повторить» старой сделки получают «нет телефона» сразу
            until = (deal.created_at or _ts(row["created_at"])) + PHONE_WAIT
            if datetime.now(timezone.utc) < until:
                reason = f"у контактов сделки пока нет телефона — ждём до {until.isoformat(timespec='seconds')}"
                if row["last_error"] != reason:
                    self.db.update(deal_id, last_error=reason)
                    self.event(deal_id, "ждём телефон", reason)
                return
            await self._no_messenger(deal_id, "no_phone", "у контактов сделки нет телефона", deal.stage_id)
            return

        if self.dry_run:
            name = names[phones[0]]
            text = self.render("first", name, deal.title)
            self.db.update(deal_id, name=name, phone=", ".join(phones), first_text=text)
            self.db.update(deal_id, status="skipped", last_error="DRY_RUN")
            self.event(deal_id, "DRY_RUN: отправил бы первое", f"{phones} → {text!r}")
            return

        if connected is None:
            connected = await self._connected()
        order = list(self.s.orbita_channels)

        self.db.update(deal_id, name=names[phones[0]], phone=", ".join(phones))
        suffix = f"-r{row['retry_n']}" if row["retry_n"] else ""
        not_found, unchecked, invalid = [], [], []
        unchecked_waits: list[datetime | None] = []
        for phone in phones:
            name = names[phone]
            text = self.render("first", name, deal.title)
            for ch in order:
                if ch not in connected:
                    unchecked.append(f"{ch}: не подключён")
                    unchecked_waits.append(None)
                    continue
                res = await self._start(deal_id, phone, ch, text, f"deal-{deal_id}-first-{phone}{suffix}-{ch}", name)
                if await self._first_done(deal_id, phone, res):
                    return
                if res.status == "invalid_phone":
                    invalid.append(phone)
                    self.event(deal_id, "Орбита не приняла номер", phone)
                    break                               # номер невалиден в любом канале — следующий номер
                if res.status == "not_found":
                    not_found.append(f"{ch}: {phone}")
                else:
                    unchecked.append(f"{ch}: {res.raw}")
                    unchecked_waits.append(res.retry_at)
            else:
                self.event(deal_id, "нет мессенджера на номере", phone)

        if len(invalid) == len(phones):
            await self._no_messenger(deal_id, "no_phone", f"Орбита не приняла номера: {', '.join(invalid)}",
                                    deal.stage_id)
            return
        if unchecked:
            # Хотя бы один выбранный канал не проверил номер — не утверждаем, что мессенджера нет. Если каждый
            # непроверенный канал назвал срок (лимит, flood wait) — ждём ближайший, не тратя попытку
            waits = [w for w in unchecked_waits if w is not None]
            retry_at = min(waits) if waits and len(waits) == len(unchecked) else None
            raise OrbitaError(f"не найден: {', '.join(not_found) or '—'}; не проверен: {'; '.join(unchecked)}",
                              retry_at)
        await self._no_messenger(deal_id, "no_messenger", f"мессенджер не найден: {', '.join(phones)}",
                                deal.stage_id)

    async def _connected(self) -> set[str]:
        connected = await self.orbita.connected_channels()
        order = list(self.s.orbita_channels)
        if not connected & set(order):
            # Не «мессенджер не найден»: иначе живой клиент уйдёт на этап NO_MESSENGER
            raise OrbitaError(f"нет подключённых каналов из {order}")
        return connected

    async def _resume_first(self, deal_id: int, row) -> tuple[bool, set[str]]:
        """Узнать исход прерванной отправки первого сообщения повтором того же запроса. True — отправка завершена
        (201 / 202, исход сопоставлен в _first_done); False — исход определён и сообщение не ушло (намерение снято
        в _start), дальше — обычная новая отправка по свежим данным.

        До повтора — только то, что запрещает сам повтор: сделка ещё в работе робота (status new — ни ручной
        остановки, ни пропуска), окно отправки (проверяет вызывающий) и 48 ч. Повтор тем же ключом не шлёт второе
        сообщение, только если Орбита первый запрос получила; не получила — повтор и есть отправка, а писать
        «вы оставили заявку» спустя двое суток нельзя. Поэтому за 48 ч повтора нет, а сделка не пропускается молча
        (сообщение могло уйти) — «проверить вручную»."""
        if self._expired(row, _ts(row["created_at"])):
            raise UnknownOutcome(f"исход отправки в {row['intent_channel']} ({row['intent_phone']}) неизвестен, "
                                 "а заявка уже старше 48 ч — повтор не отправляю")
        connected = await self._connected()
        # В другой канал нельзя — клиент получит два первых. Тело — исходное целиком: канал, номер, ключ, текст и имя
        # (first_text и name записаны той же попыткой до запроса); имя из Битрикса могли поправить, а с другим телом
        # Орбита ответит idempotency_key_reused
        if row["intent_channel"] not in connected:
            raise UnknownOutcome(f"исход отправки в {row['intent_channel']} ({row['intent_phone']}) неизвестен, "
                                 f"а канал сейчас не подключён")
        res = await self._start(deal_id, row["intent_phone"], row["intent_channel"], row["first_text"],
                                row["intent_key"], row["name"] or "")
        return await self._first_done(deal_id, row["intent_phone"], res), connected

    async def _start(self, deal_id: int, phone: str, channel: str, text: str, key: str,
                     name: str) -> FirstSendResult:
        """Один запрос «написать первым». Намерение (номер, канал, ключ) пишется в базу ДО запроса — той же записью,
        что текст и имя этого запроса (first_text, name): повтор после неизвестного исхода несёт их, а у номеров
        разных контактов сделки имена разные. Намерение
        стирается, только когда исход определён: отказ Орбиты, по которому точно ничего не ушло, или ответ
        «не найден» / «не проверен» / «номер не принят» — здесь; ушло или могло уйти (201/202) — в _first_done
        той же записью, что и результат (иначе обрыв между двумя записями оставит сделку без намерения и без
        результата). Сбой связи, 5xx, idempotency_in_progress оставляют намерение — повтор пойдёт туда же."""
        self.db.update(deal_id, intent_phone=phone, intent_channel=channel, intent_key=key, first_text=text, name=name)
        try:
            res = await self.orbita.start_dialog(channel, phone, text, key, name or None)
        except OrbitaAPIError as e:
            if e.key_reused:                            # прежний запрос с этим ключом мог отправить — нужен человек
                raise UnknownOutcome(f"{phone} ({channel}): {e}") from e
            if e.definite:
                self.db.update(deal_id, intent_phone=None, intent_channel=None, intent_key=None)
            raise
        except OrbitaError:
            raise                                       # сбой связи: повтор с тем же ключом безопасен
        except Exception as e:                          # непредвиденный сбой посреди отправки
            raise UnknownOutcome(f"{phone}: {type(e).__name__}: {e}") from e
        if res.status not in ("sent", "pending"):
            self.db.update(deal_id, intent_phone=None, intent_channel=None, intent_key=None)
        return res

    async def _first_done(self, deal_id: int, phone: str, res: FirstSendResult) -> bool:
        """Сообщение ушло (или могло уйти, 202) — записать и закончить перебор."""
        if res.status == "sent":
            self._mark_sent_first(deal_id, phone, res.dialog_id, res.channel)
            await self.note(deal_id, f"Написал клиенту первым ({res.channel or 'мессенджер'}, {phone}).")
            await self._restore_stage(deal_id)
            return True
        if res.status == "pending":
            after = self._handed_over(deal_id, "first", res.request_key, "первое сообщение передано, ждём подтверждения",
                                      f"{phone} {res.channel} {res.request_key}",
                                      status="pending_first", phone=phone, channel=res.channel, last_error=None,
                                      pending_at=now(), intent_phone=None, intent_channel=None, intent_key=None)
            await self._after_outcome(after)
            return True
        return False

    @staticmethod
    def _expired(row, created: datetime | None) -> bool:
        start = max(filter(None, (created, _ts(row["retried_at"]))), default=None)
        return start is not None and datetime.now(timezone.utc) - start > MAX_AGE

    def _too_old(self, deal_id: int, row, created: datetime | None) -> bool:
        if not self._expired(row, created):
            return False
        self.db.update(deal_id, status="skipped", last_error="заявка старше 48 ч")
        self.event(deal_id, "пропущена", "заявка старше 48 ч")
        return True

    def _first_sent(self, sent_at: datetime, dialog_id: str | None, channel: str | None) -> dict:
        """Факт доставки первого сообщения: диалог, канал, момент, срок напоминания; намерение снято."""
        due = sent_at + timedelta(hours=self.s.followup_hours)
        return dict(dialog_id=dialog_id, channel=channel, first_sent_at=sent_at.isoformat(timespec="seconds"),
                    followup_due_at=due.isoformat(timespec="seconds"),
                    intent_phone=None, intent_channel=None, intent_key=None)

    def _mark_sent_first(self, deal_id: int, phone: str, dialog_id: str | None, channel: str | None) -> None:
        """201: первое сообщение ушло. Отправка, переданная до «Повторить» (202), больше исхода не ждёт — факт
        доставки уже есть."""
        self.db.update(deal_id, status="sent_first", phone=phone, last_error=None, request_key=None, pending_at=None,
                       **self._first_sent(datetime.now(timezone.utc), dialog_id, channel))
        self.event(deal_id, "первое сообщение отправлено", f"{phone} {channel or ''}")

    async def _restore_stage(self, deal_id: int) -> None:
        """Первое сообщение ушло после ручного «Повторить», а сделку раньше робот перенёс на «Не нашёл
        мессенджер», — вернуть её на прежний этап. Менеджер уже сам передвинул сделку — не трогаем.

        Это сохранённая работа (stage_before при first_sent_at): кроме вызова сразу после отправки её доделывает
        шаг прохода stages — после сбоя Битрикса (пауза 2, 4, 8, 16 мин) и после перезапуска. Отправку
        сбой не повторяет: сообщение уже ушло. MAX_ATTEMPTS сбоев подряд — комментарий «верните вручную»."""
        row = self.db.deal(deal_id)
        if row is None or not row["stage_before"] or not row["first_sent_at"] or self.dry_run:
            return
        if row["stage_retry_at"] and datetime.now(timezone.utc) < _ts(row["stage_retry_at"]):
            return
        before = row["stage_before"]
        done = dict(stage_before=None, stage_attempts=0, stage_retry_at=None)
        try:
            deal = await self.bitrix.get_deal(deal_id)
            if deal.stage_id != self.s.no_messenger_stage:
                self.db.update(deal_id, **done)
                self.event(deal_id, "этап не возвращён", f"сделка уже на этапе {deal.stage_id}")
                return
            await self.bitrix.set_stage(deal_id, before)
        except Exception as e:  # noqa: BLE001
            retry_at = getattr(e, "retry_at", None)
            if retry_at is not None:
                # Лимит Битрикса назвал срок — ждём его, попытку возврата не тратим
                self.db.update(deal_id, stage_retry_at=retry_at.isoformat(timespec="seconds"))
                self.event(deal_id, "возврат этапа отложен", f"до {retry_at.isoformat(timespec='seconds')}: {e}")
                return
            attempts = (row["stage_attempts"] or 0) + 1
            log.error("сделка %s: не удалось вернуть этап %s (попытка %s): %s: %s", deal_id, before, attempts,
                      type(e).__name__, e, exc_info=e)
            self.event(deal_id, "не удалось вернуть этап", f"{before}, попытка {attempts}: {type(e).__name__}: {e}")
            if attempts >= MAX_ATTEMPTS or getattr(e, "permanent", False):
                self.db.update(deal_id, **done)
                await self.note(deal_id, f"Первое сообщение ушло, но вернуть сделку с этапа «Не нашёл мессенджер» "
                                         f"на прежний ({before}) не удалось — верните вручную.")
                return
            pause = datetime.now(timezone.utc) + RETRY_PAUSE * 2 ** (attempts - 1)
            self.db.update(deal_id, stage_attempts=attempts, stage_retry_at=pause.isoformat(timespec="seconds"))
            return
        self.db.update(deal_id, **done)
        self.event(deal_id, f"этап возвращён → {before}", "первое сообщение ушло после «Повторить»")
        await self.note(deal_id, "Первое сообщение ушло — вернул сделку с этапа «Не нашёл мессенджер» на прежний.")

    async def _no_messenger(self, deal_id: int, status: str, reason: str, stage_now: str) -> None:
        if not self.dry_run:
            if stage_now != self.s.no_messenger_stage:
                # Запоминаем, откуда увели, ДО переноса: после успешного «Повторить» вернём. Обрыв между переносом
                # и записью статуса не теряет прежний этап (повтор, когда сделка уже на «Не нашёл мессенджер»,
                # прежний этап не затирает)
                self.db.update(deal_id, stage_before=stage_now)
            await self.bitrix.set_stage(deal_id, self.s.no_messenger_stage)
        self.db.update(deal_id, status=status, last_error=reason)
        self.event(deal_id, "этап → «Не нашёл мессенджер»", reason)
        await self.note(deal_id, f"Не смог написать клиенту: {reason}. Нужен звонок менеджера.")

    # ---------- напоминание ----------

    async def followup(self, deal_id: int) -> None:
        row = self.db.deal(deal_id)
        if row is None or row["status"] != "sent_first":
            return
        if datetime.now(timezone.utc) < _ts(row["followup_due_at"]):
            return
        if self.s.followup_on and not self.in_window():
            return

        if not row["dialog_id"]:
            self.db.update(deal_id, status="needs_check", last_error="нет номера диалога Орбиты")
            self.event(deal_id, "не понял, ответил ли клиент", "нет номера диалога")
            return
        deal = await self.bitrix.get_deal(deal_id)
        if deal.stage_semantic != "P":
            self.db.update(deal_id, status="skipped", last_error=f"сделка закрыта ({deal.stage_id})")
            self.event(deal_id, "напоминание не нужно", f"сделка закрыта ({deal.stage_id})")
            return

        replied = client_replied(await self.orbita.history(row["dialog_id"], _ts(row["first_sent_at"])), _ts(row["first_sent_at"]))
        if replied is None:
            self.db.update(deal_id, status="needs_check", last_error="не смог прочитать историю диалога")
            self.event(deal_id, "не понял, ответил ли клиент")
            await self.note(deal_id, "Не смог проверить, ответил ли клиент. Напоминание не отправлял.")
            return
        if replied:
            await self._client_replied(deal_id, "по истории")
            return
        if datetime.now(timezone.utc) - _ts(row["followup_due_at"]) > MAX_AGE:
            # Робот или Орбита стояли: «напоминаю о заявке» через несколько суток — хуже, чем ничего
            self.db.update(deal_id, status="no_reply", last_error="напоминание просрочено больше чем на 48 ч")
            self.event(deal_id, "клиент не ответил", "напоминание не отправлено: просрочено больше чем на 48 ч")
            await self.note(deal_id, "Клиент не ответил. Напоминание не отправлял: его срок прошёл больше 48 ч "
                                     "назад — дальше вручную.")
            return
        if not self.s.followup_on:
            self.db.update(deal_id, status="no_reply")
            self.event(deal_id, "клиент не ответил", "напоминание выключено")
            await self.note(deal_id, f"Клиент не ответил за {self.s.followup_hours:g} ч. "
                                     "Напоминание выключено — дальше вручную.")
            return

        res = await self._send_step(row, "followup", self.render("followup", row["name"] or "", deal.title),
                                    "напоминание")
        sent = dict(status="sent_followup", followup_sent_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if res.status == "pending":
            after = self._handed_over(deal_id, "followup", res.request_key, "напоминание передано, ждём подтверждения",
                                      res.request_key or "", **sent)
            await self.note(deal_id, f"Клиент не ответил за {self.s.followup_hours:g} ч — передал напоминание "
                                     "в мессенджер.")
            await self._after_outcome(after)
        else:
            self.db.update(deal_id, followup_request_key=None, **sent)
            self.event(deal_id, "напоминание отправлено")
            await self.note(deal_id, f"Клиент не ответил за {self.s.followup_hours:g} ч — отправил напоминание.")

    async def _send_step(self, row, step: str, fresh_text: str, title: str) -> ReplyResult:
        """Напоминание или ответ по шаблону. Намерение — ключ и текст (<step>_key, <step>_text) — пишется до запроса
        и стирается только при определённом отказе Орбиты: тогда «Повторить» выпустит новый -r<n>. Сбой связи, 5xx
        и непредвиденный сбой намерение оставляют — повтор (автоматический и ручной) идёт тем же ключом и с тем же
        текстом, даже если шаблон или название сделки с тех пор поменялись. fresh_text — только для нового ключа
        (и для ключа, сохранённого версией без текста)."""
        deal_id = row["deal_id"]
        key_col, text_col = f"{step}_key", f"{step}_text"
        if row[key_col]:
            key, text = row[key_col], row[text_col] or fresh_text
        else:
            n = row[f"{step}_retry_n"]
            key, text = f"deal-{deal_id}-{step}" + (f"-r{n}" if n else ""), fresh_text
        self.db.update(deal_id, **{key_col: key, text_col: text})
        try:
            return await self.orbita.send_reply(row["dialog_id"], text, key)
        except OrbitaAPIError as e:
            if e.key_reused:
                # Ключ уже приходил с другим телом: прежний запрос мог отправить — исход выясняет человек. Исход нашего
                # тела этот ключ не вернёт никогда, хранить его незачем: снимается той же записью, что ставит
                # «проверить вручную», и «Повторить» пойдёт новым -r<n> (панель предупредит о втором сообщении)
                raise UnknownOutcome(f"{title}: {e}", **{key_col: None, text_col: None, "reused_key_step": step}) from e
            if e.definite:
                self.db.update(deal_id, **{key_col: None, text_col: None})
            raise
        except OrbitaError:
            raise
        except Exception as e:
            raise UnknownOutcome(f"{title}: {type(e).__name__}: {e}") from e

    async def after_followup(self, deal_id: int) -> None:
        """Через followup_hours после напоминания — итог: ответил или нет. Иначе статус висит вечно."""
        row = self.db.deal(deal_id)
        if row is None or row["status"] != "sent_followup" or not row["followup_sent_at"]:
            return
        if datetime.now(timezone.utc) < _ts(row["followup_sent_at"]) + timedelta(hours=self.s.followup_hours):
            return
        if row["followup_request_key"]:
            # Напоминание передано (202), доставка не подтверждена: итог «молчит» по нему не выносим. Ждём
            # message.sent / message.failed; не придёт — «проверить вручную» по таймауту (send_timeout)
            return
        replied = client_replied(await self.orbita.history(row["dialog_id"], _ts(row["first_sent_at"])), _ts(row["first_sent_at"])) \
            if row["dialog_id"] else None
        if replied:
            await self._client_replied(deal_id, "по истории")
            return
        if replied is None:
            self.db.update(deal_id, status="needs_check", last_error="не смог прочитать историю диалога")
            self.event(deal_id, "не понял, ответил ли клиент на напоминание")
            return
        self.db.update(deal_id, status="no_reply", last_error=None)
        self.event(deal_id, "клиент не ответил", "и на напоминание")
        await self.note(deal_id, "Клиент не ответил и на напоминание — дальше вручную.")

    # ---------- клиент ответил ----------

    async def _client_replied(self, deal_id: int, how: str) -> None:
        """Клиент написал после нашего сообщения: робот больше не пишет, кроме ответа по шаблону."""
        self._set_replied(deal_id, how)
        await self.note(deal_id, "Клиент ответил в мессенджере.")

    def _set_replied(self, deal_id: int, how: str) -> None:
        """Запись «клиент ответил» без внешних вызовов — её можно включить в атомарный переход."""
        row = self.db.deal(deal_id)
        fields: dict = {"status": "replied", "client_replied_at": now()}
        if self.s.reply_on and not row["reply_sent_at"] and not self.dry_run:
            due = datetime.now(timezone.utc) + timedelta(minutes=self.s.reply_delay_minutes)
            fields["reply_due_at"] = due.isoformat(timespec="seconds")
        self.db.update(deal_id, **fields)
        self.event(deal_id, f"клиент ответил ({how})")

    async def poll_replies(self, force: bool = False) -> None:
        """Без вебхуков Орбиты: раз в REPLY_POLL смотрим историю свежих диалогов (force — не ждать паузу)."""
        for candidate in self.db.by_status("sent_first", "sent_followup"):
            async with self.lock:
                if self.active:
                    await self._poll_reply(candidate["deal_id"], force)

    async def _poll_reply(self, deal_id: int, force: bool) -> None:
        row = self.db.deal(deal_id)
        if row is None or row["status"] not in ("sent_first", "sent_followup"):
            return
        sent = _ts(row["first_sent_at"])
        if not row["dialog_id"] or not sent or datetime.now(timezone.utc) - sent > REPLY_POLL_DAYS:
            return
        if not force and row["checked_at"] and _ts(row["checked_at"]) > datetime.now(timezone.utc) - REPLY_POLL:
            return
        self.db.update(deal_id, checked_at=now())
        try:
            replied = client_replied(await self.orbita.history(row["dialog_id"], sent), sent)
        except Exception as e:  # noqa: BLE001
            log.warning("сделка %s: история недоступна: %s", deal_id, e)
            return
        if replied:
            await self._client_replied(deal_id, "опрос")

    async def auto_reply(self, deal_id: int) -> None:
        """Ответ клиенту по шаблону: один раз, если менеджер не успел ответить сам."""
        row = self.db.deal(deal_id)
        if row is None or row["status"] != "replied" or row["reply_sent_at"] or not row["reply_due_at"]:
            return
        if not self.s.reply_on:
            self.db.update(deal_id, reply_due_at=None)
            return
        history = await self.orbita.history(row["dialog_id"], _ts(row["first_sent_at"]))
        client_msg = first_incoming(history, _ts(row["first_sent_at"]))
        if client_msg is None:
            self.db.update(deal_id, reply_due_at=None)
            self.event(deal_id, "ответ по шаблону не нужен", "не нашёл сообщение клиента в истории")
            return
        if answered_after(history, client_msg.at):
            self.db.update(deal_id, reply_due_at=None)
            self.event(deal_id, "ответ по шаблону не нужен", "менеджер уже ответил")
            return
        # Отсечка — от момента, когда робот должен был ответить (сообщение клиента + пауза из настроек), а не
        # от самого сообщения: иначе при паузе от 30 мин ответ почти никогда не уходил бы. После ручного
        # «Повторить» — от него, если он позже
        should_answer_at = max(filter(None, (client_msg.at + timedelta(minutes=self.s.reply_delay_minutes),
                                             _ts(row["reply_retried_at"]))))
        if datetime.now(timezone.utc) - should_answer_at > REPLY_MAX_LATE:
            self.db.update(deal_id, reply_due_at=None)
            self.event(deal_id, "ответ по шаблону не отправлен", "опоздал с ответом больше чем на 30 мин")
            return
        res = await self._send_step(row, "reply", self.render("reply", row["name"] or ""), "ответ по шаблону")
        sent = dict(reply_sent_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), reply_due_at=None,
                    attempts=0)
        if res.status == "pending":
            after = self._handed_over(deal_id, "reply", res.request_key, "ответ по шаблону передан, ждём подтверждения",
                                      res.request_key or "", **sent)
            await self.note(deal_id, "Передал клиенту ответ по шаблону. Дальше — менеджер.")
            await self._after_outcome(after)
        else:
            self.db.update(deal_id, reply_request_key=None, **sent)
            self.event(deal_id, "ответ по шаблону отправлен")
            await self.note(deal_id, "Ответил клиенту по шаблону. Дальше — менеджер.")

    # ---------- вебхуки Орбиты ----------

    async def process_inbox(self) -> None:
        """Разобрать сохранённые события Орбиты (таблица orbita_events, state='new').

        Событие помечается обработанным только после успешного on_orbita_event; сбой оставляет его в
        очереди до следующего прохода, после MAX_ATTEMPTS сбоев — failed. Отмена (остановка сервиса)
        оставляет событие в очереди: после перезапуска его подхватит первый проход. Обработка
        идемпотентна — каждое действие проверяет статус сделки, исход отправки снимает ключ, по которому применён.
        Исход отправки, ключа которой ещё нет ни в одной сделке, — не потерян: он сохранён в send_outcomes
        (_send_outcome), и событие на этом сделало своё."""
        async with self._inbox:
            for row in self.db.pending_events():
                try:
                    ev = json.loads(row["body"] or "")
                except ValueError as e:
                    self.db.event_failed(row["id"], f"тело не JSON: {e}", give_up=True)
                    log.error("событие Орбиты %s: тело не JSON", row["id"])
                    continue
                try:
                    await self.on_orbita_event(ev)
                except Exception as e:  # noqa: BLE001
                    attempts = row["attempts"] + 1
                    self.db.event_failed(row["id"], f"{type(e).__name__}: {e}", give_up=attempts >= MAX_ATTEMPTS)
                    log.exception("событие Орбиты %s (%s): ошибка обработки, попытка %s", row["id"], row["type"],
                                  attempts)
                    self.event(None, "Орбита: ошибка обработки события",
                               f"{row['type']} #{row['id']}, попытка {attempts}: {e}")
                else:
                    self.db.event_done(row["id"])

    async def on_orbita_event(self, ev: dict) -> None:
        """Событие вебхука Орбиты (подпись уже проверена, дубли отсечены).

        message.received — клиент ответил: напоминание больше не нужно, сразу, без ожидания.
        message.sent / message.failed — исход отправки, на которую API ответил 202.
        Остальное только пишем в журнал.
        """
        kind, data = ev.get("type", ""), ev.get("data") or {}
        if ev.get("is_test"):
            self.event(None, "Орбита: тестовое событие", kind)
            return
        async with self.lock:
            if kind == "message.received" and data.get("direction", "incoming") == "incoming":
                row = self.db.deal_by("dialog_id", data.get("dialog_id"))
                # «Проверить вручную» по таймауту подтверждения напоминания (send_timeout): первое сообщение
                # подтверждено, диалог известен, клиент ещё не отвечал — его сообщение остаётся ответом
                reminder_unconfirmed = row and row["status"] == "needs_check" and row["followup_request_key"] \
                    and row["followup_sent_at"] and not row["client_replied_at"]
                if row and (row["status"] in ("sent_first", "sent_followup", "no_reply") or reminder_unconfirmed):
                    await self._client_replied(row["deal_id"], "вебхук")
            elif kind in ("message.sent", "message.failed") and data.get("request_key"):
                await self._send_outcome(kind == "message.sent", data)
            elif kind in ("channel.disconnected", "channel.connected"):
                self.event(None, f"Орбита: {kind}", str(data.get("provider") or data.get("id") or ""))


    # ---------- исход отправки по request_key ----------
    #
    # Одна точка сопоставления исходов (message.sent / message.failed) с отправками первого сообщения, напоминания
    # и ответа по шаблону — по request_key, а не по статусу сделки:
    # - исход сохраняется (send_outcomes) и сразу применяется к отправке, чей ключ уже записан в сделку;
    # - запись ключа любой отправки (_handed_over) той же транзакцией сверяется с уже принятым исходом — исход,
    #   пришедший раньше, чем робот узнал ключ (потерян ответ 202, обрыв процесса), не теряется;
    # - факт доставки (первое: first_sent_at, диалог, канал; напоминание и ответ: *_sent_at) и снятие ключа
    #   записываются по ключу в любом статусе;
    # - статус меняется только из состояний, где робот ещё ждёт этот шаг, и никогда — при ручном решении
    #   (manual_stop_at: «Остановить робота», «Клиент ответил»): тогда только факт и запись в журнал.

    def _handed_over(self, deal_id: int, step: str, request_key: str | None, title: str, detail: str,
                     **fields) -> "_AfterOutcome | None":
        """Отправка передана (202): ключ ожидания и поля шага пишутся одной транзакцией со сверкой с исходом этого
        ключа, если он уже принят. Внешние действия по исходу — после транзакции (_after_outcome)."""
        with self.db.tx():
            self.db.update(deal_id, **{self.db.SEND_KEYS[step]: request_key}, **fields)
            self.event(deal_id, title, detail)
            return self._match_outcome(request_key) if request_key else None

    async def _send_outcome(self, ok: bool, data: dict) -> None:
        """Вебхук с исходом отправки, на которую Орбита ответила 202: сохранить и применить по request_key."""
        rk = str(data["request_key"])
        dialog = str(data["dialog_id"]) if data.get("dialog_id") is not None else None
        with self.db.tx():
            self.db.save_outcome(rk, ok, str(data.get("reason_code") or "причина не указана"), dialog,
                                 str(data["sent_at"]) if data.get("sent_at") else None)
            after = self._match_outcome(rk)
        if after is None:
            log.info("исход отправки %s: отправки с этим ключом пока нет — сохранён до записи ключа", rk)
        await self._after_outcome(after)

    def _match_outcome(self, request_key: str) -> "_AfterOutcome | None":
        """Применить принятый исход к отправке с этим ключом. Без внешних вызовов — часть транзакции вызывающего.
        None — исхода ещё нет или ни одна отправка этот ключ не ждёт (исход остаётся неприменённым)."""
        found = self.db.deal_by_send_key(request_key)
        o = self.db.outcome(request_key)
        if found is None or o is None:
            return None
        row, step = found
        after = self._apply_first(row, o) if step == "first" else self._apply_step(row, step, o)
        self.db.outcome_applied(request_key)
        return after

    async def _after_outcome(self, after: "_AfterOutcome | None") -> None:
        if after is None:
            return
        for text in after.notes:
            await self.note(after.deal_id, text)
        if after.restore_stage:
            await self._restore_stage(after.deal_id)

    def _apply_first(self, row, o) -> _AfterOutcome:
        deal_id = row["deal_id"]
        after = _AfterOutcome(deal_id)
        # Робот ещё пытается доставить первое сообщение: ждёт 202, «проверить вручную» по таймауту, «Повторить» нажат
        # и новая попытка не передана, сбои новой попытки. Остальное (ручная остановка, «Клиент ответил», пропуск,
        # «Не нашёл мессенджер» новой попытки) — решение, которое исход не отменяет
        waiting = row["status"] in ("pending_first", "needs_check", "new", "error") and not row["manual_stop_at"]
        fields: dict = {"request_key": None, "pending_at": None}
        if not o["ok"]:
            reason = o["reason"] or "причина не указана"
            changes = waiting and row["status"] in ("pending_first", "needs_check")
            if changes:
                fields.update(status="needs_check", last_error=f"не доставлено: {reason}")
                after.notes.append(f"Первое сообщение не доставлено ({reason}). Нужен звонок менеджера.")
            self.db.update(deal_id, **fields)
            self.event(deal_id, "первое сообщение не доставлено", reason if changes else
                       f"{reason}; статус «{row['status']}» не меняется")
            return after
        if row["first_sent_at"]:
            self.db.update(deal_id, **fields)                  # факт уже записан (повтор события, старая версия)
            return after
        dialog = o["dialog_id"] or row["dialog_id"]
        # Момент отправки — из события, иначе — когда Орбита ответила 202 (не момент подтверждения: от него
        # считается, что ответ клиента пришёл после нашего сообщения)
        sent_at = _parse_time(o["sent_at"]) or _ts(row["pending_at"]) or datetime.now(timezone.utc)
        fields.update(self._first_sent(sent_at, dialog, row["channel"]))
        after.restore_stage = True              # этап «Не нашёл мессенджер» больше не правда — в любом статусе
        if not waiting:
            self.db.update(deal_id, **fields)
            self.event(deal_id, "первое сообщение: мессенджер подтвердил доставку",
                       f"статус «{row['status']}» не меняется")
            return after
        self.db.update(deal_id, status="sent_first", last_error=None, **fields)
        self.event(deal_id, "первое сообщение отправлено", f"{row['phone'] or ''} {row['channel'] or ''}")
        after.notes.append(f"Написал клиенту первым ({row['channel']}, {row['phone']}).")
        # Клиент мог ответить раньше подтверждения: message.received пришёл, когда номер диалога был ещё неизвестен,
        # и ни к какой сделке не подошёл. Клиент ответил ⇒ первое сообщение дошло
        since = (_ts(row["pending_at"]) - CLOCK_SKEW).isoformat(timespec="seconds") if row["pending_at"] else None
        if dialog and since and self.db.incoming_since(dialog, since):
            self._set_replied(deal_id, "вебхук, до подтверждения отправки")
            after.notes.append("Клиент ответил в мессенджере.")
        return after

    def _apply_step(self, row, step: str, o) -> _AfterOutcome:
        """Исход напоминания (followup) или ответа по шаблону (reply)."""
        deal_id = row["deal_id"]
        after = _AfterOutcome(deal_id)
        title = "напоминание" if step == "followup" else "ответ по шаблону"
        sent_col, waiting = f"{step}_sent_at", ("sent_followup" if step == "followup" else "replied")
        # Робот ещё ведёт этот шаг: не ручное решение; напоминание после ответа клиента значения не имеет
        mine = not row["manual_stop_at"] and (step == "reply" or not row["client_replied_at"])
        # «Повторить» нажат, повтор той же отправки ещё не ушёл: статус там, откуда шаг отправляется, момент отправки
        # стёрт, ключ прежней отправки сохранён (_retry)
        retrying = mine and not row[sent_col] and row["status"] == ("sent_first" if step == "followup" else "replied")
        fields: dict = {self.db.SEND_KEYS[step]: None}
        if o["ok"]:
            if not row[sent_col]:
                fields[sent_col] = (_parse_time(o["sent_at"]) or datetime.now(timezone.utc)).isoformat(timespec="seconds")
                if step == "reply":
                    fields["reply_due_at"] = None
            # «Проверить вручную» (таймаут подтверждения, нечитаемая история после «Повторить»), сбои после
            # «Повторить» или сам повтор: шаг сделан, повтор не нужен
            changes = retrying or (mine and row["status"] in ("needs_check", "error"))
            if changes:
                fields.update(status=waiting, last_error=None)
            self.db.update(deal_id, **fields)
            self.event(deal_id, f"{title}: мессенджер подтвердил доставку",
                       "" if changes or row["status"] == waiting else f"статус «{row['status']}» не меняется")
            return after
        # Сообщение не ушло: шаг не сделан — момент отправки и намерение снимаются в любом статусе («Повторить» после
        # отказа пойдёт новым -r<n>)
        reason = o["reason"] or "причина не указана"
        fields.update({sent_col: None, f"{step}_key": None, f"{step}_text": None})
        if retrying:
            # Человек уже решил повторить: прежняя отправка точно не ушла — повтор пойдёт новым ключом (с прежним
            # Орбита вернула бы этот же отказ); статус и срок ответа не трогаем
            fields[f"{step}_retry_n"] = (row[f"{step}_retry_n"] or 0) + 1
        # Итог «не ответил» снимается, только если в нём учтено это напоминание (followup_sent_at): итог, вынесенный
        # после «Повторить» без повторной отправки, на прежнем напоминании не основан
        act = mine and not retrying and (row["status"] in (waiting, "needs_check")
                                         or (step == "followup" and row["status"] == "no_reply" and row[sent_col]))
        if act:
            fields.update(status="needs_check", last_error=f"{title} не доставлен(о): {reason}")
        if step == "reply" and mine and not retrying:
            fields["reply_due_at"] = now()                      # ответ всё ещё нужен — «Повторить» отправит
        self.db.update(deal_id, **fields)
        self.event(deal_id, f"{title} не доставлен(о)", reason if act else
                   f"{reason}; статус «{row['status']}» не меняется")
        if act:
            after.notes.append(f"Не удалось доставить {title} ({reason}). Проверьте переписку; "
                               "«Повторить» в панели робота отправит заново.")
        return after


def render(tpl: str, name: str, deal_title: str = "") -> str:
    """Подстановка {name} и {deal_title}. Ошибка в шаблоне — KeyError / ValueError / IndexError."""
    return tpl.strip().format(name=name, deal_title=deal_title)
