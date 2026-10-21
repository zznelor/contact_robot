"""Логика первого контакта.

1. Новая сделка → первое сообщение клиенту в мессенджер через Орбиту.
2. Мессенджер не найден → этап сделки «Не нашёл мессенджер».
3. Клиент молчит N часов → напоминание (если включено).
4. Клиент ответил → один ответ по шаблону (если включён), дальше работает менеджер.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from . import config
from .bitrix import Bitrix
from .config import Env, Settings
from .db import DB, now
from .orbita import FirstSendResult, OrbitaError, answered_after, client_replied, first_incoming
from .orbita_api import OrbitaAPI, OrbitaAPIError

log = logging.getLogger("first_contact")

MAX_ATTEMPTS = 5
# Пауза перед следующей попыткой после сбоя шага: 2, 4, 8, 16 мин — пятая (последняя) примерно через полчаса
RETRY_PAUSE = timedelta(minutes=2)
# Сделки, которые ждали дольше (робот был выключен или лежала Орбита), не трогаем:
# писать «вы оставили заявку» через неделю — плохо.
MAX_AGE = timedelta(hours=48)
# Орбита ответила 202 на «написать первым», а вебхука с исходом нет так долго — зовём человека
PENDING_TIMEOUT = timedelta(minutes=30)
# Ответ по шаблону имеет смысл, только пока клиент ещё в разговоре
REPLY_MAX_LATE = timedelta(minutes=30)
# Без вебхуков Орбиты ответ клиента ищем опросом истории — не чаще, чем раз в столько
REPLY_POLL = timedelta(minutes=5)
REPLY_POLL_DAYS = timedelta(days=3)


# Какие статусы можно вернуть в работу кнопкой «Повторить»
RETRYABLE = {"error", "needs_check", "skipped", "no_messenger", "no_phone"}


class UnknownOutcome(Exception):
    """Отправка могла уйти, а могла и нет. Повторять нельзя — нужен человек."""


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
        "poll": "Забрать новые сделки из Битрикса",
        "first": "Написать первым по новым сделкам",
        "followup": "Напоминания и итог по тем, кто молчит",
        "pending": "Проверить неподтверждённые отправки",
        "replies": "Проверить ответы клиентов в истории",
        "auto_reply": "Отправить ответы по шаблону",
    }

    async def tick(self) -> None:
        async with self._pass:
            # Принятые события Орбиты разбираются и при выключенном роботе: они только записывают исходы
            await self._isolated("events")
            if not self.active:
                return
            for step in self.STEPS:
                if step != "events":
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
            self.db.update(deal_id, status="needs_check", last_error=str(e))
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
            status = "error" if attempts >= MAX_ATTEMPTS else (row["status"] if row else "new")
            pause = datetime.now(timezone.utc) + RETRY_PAUSE * 2 ** (attempts - 1)
            self.db.update(deal_id, attempts=attempts, last_error=f"{type(e).__name__}: {e}", status=status,
                           next_attempt_at=None if status == "error" else pause.isoformat(timespec="seconds"))
            self.event(deal_id, "ошибка", f"попытка {attempts}: {e}")
            if status == "error":
                log.error("сделка %s: ошибка после %s попыток: %s: %s", deal_id, attempts, type(e).__name__, e)
                await self.note(deal_id, f"Не смог обработать заявку после {attempts} попыток: {e}")
        else:
            # Сделка продвинулась — прошлые сбои не должны копиться до «ошибки» на следующем шаге
            after = self.db.deal(deal_id)
            if before and after and (after["attempts"] or after["next_attempt_at"]) \
                    and after["status"] != before["status"]:
                self.db.update(deal_id, attempts=0, next_attempt_at=None)

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
        if row["client_replied_at"]:
            status = "replied"
        elif row["followup_sent_at"]:
            status = "sent_followup"
        elif row["first_sent_at"]:
            status = "sent_first"
        else:
            status = "new"
        fields: dict = {"status": status, "attempts": 0, "last_error": None, "next_attempt_at": None}
        # Повтор шага после ошибки — новый ключ идемпотентности этого шага (-r<n>): с прежним Орбита вернула
        # бы исход прошлой попытки и сообщение не ушло бы
        if status == "sent_first":
            fields["followup_retry_n"] = (row["followup_retry_n"] or 0) + 1
        if status == "replied" and not row["reply_sent_at"] and row["reply_due_at"] and self.s.reply_on:
            fields.update(reply_retry_n=(row["reply_retry_n"] or 0) + 1, reply_due_at=now())
        if status == "new":
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
                self.db.update(deal_id, reply_due_at=None)
            else:
                self.db.update(deal_id, status="skipped", last_error="остановлено вручную", reply_due_at=None)
            self.event(deal_id, "вручную: остановить", who)
            return True

    async def mark_replied(self, deal_id: int, who: str) -> bool:
        """«Клиент ответил» из панели: робот больше не пишет, ответ по шаблону не отправляется."""
        async with self.lock:
            row = self.db.deal(deal_id)
            if row is None:
                return False
            self.db.update(deal_id, status="replied", reply_due_at=None,
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
        if datetime.now(timezone.utc) - _ts(row["updated_at"]) <= PENDING_TIMEOUT:
            return
        self.db.update(deal_id, status="needs_check", last_error="нет подтверждения отправки от Орбиты")
        self.event(deal_id, "нет подтверждения отправки", row["request_key"] or "")
        await self.note(deal_id, "Орбита не подтвердила, что первое сообщение ушло. Проверьте переписку.")

    # ---------- первое сообщение и «нет мессенджера» ----------

    async def first_contact(self, deal_id: int) -> None:
        row = self.db.deal(deal_id)
        if row is None or row["status"] != "new":
            return
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
        manual = bool(row["retry_n"])      # человек нажал «Повторить» — этап робота не помеха
        if deal.stage_semantic != "P" or (deal.stage_id == self.s.no_messenger_stage and not manual):
            self.db.update(deal_id, status="skipped", last_error=f"этап {deal.stage_id}")
            self.event(deal_id, "пропущена", f"этап {deal.stage_id}")
            return

        name, phones = ("", [])
        if deal.contact_id:
            name, phones = await self.bitrix.get_contact(deal.contact_id)
        if not phones:
            await self._no_messenger(deal_id, "no_phone", "в контакте нет телефона")
            return

        if self.dry_run:
            text = self.render("first", name, deal.title)
            self.db.update(deal_id, name=name, phone=", ".join(phones), first_text=text)
            self.db.update(deal_id, status="skipped", last_error="DRY_RUN")
            self.event(deal_id, "DRY_RUN: отправил бы первое", f"{phones} → {text!r}")
            return

        connected = await self.orbita.connected_channels()
        order = list(self.s.orbita_channels)
        if not connected & set(order):
            # Не «мессенджер не найден»: иначе живой клиент уйдёт на этап NO_MESSENGER
            raise OrbitaError(f"нет подключённых каналов из {order}")

        if row["intent_key"]:
            # Прошлая попытка оборвалась с неизвестным исходом: сообщение могло уйти. Узнаём исход повтором
            # того же запроса (тот же канал, ключ и текст) — в другой канал нельзя, клиент получит два первых
            if row["intent_channel"] not in connected:
                raise UnknownOutcome(f"исход отправки в {row['intent_channel']} ({row['intent_phone']}) неизвестен, "
                                     f"а канал сейчас не подключён")
            res = await self._start(deal_id, row["intent_phone"], row["intent_channel"], row["first_text"],
                                    row["intent_key"], name)
            if await self._first_done(deal_id, row["intent_phone"], res):
                return

        text = self.render("first", name, deal.title)
        self.db.update(deal_id, name=name, phone=", ".join(phones), first_text=text)
        suffix = f"-r{row['retry_n']}" if row["retry_n"] else ""
        not_found, unchecked, invalid = [], [], []
        unchecked_waits: list[datetime | None] = []
        for phone in phones:
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
            await self._no_messenger(deal_id, "no_phone", f"Орбита не приняла номера: {', '.join(invalid)}")
            return
        if unchecked:
            # Хотя бы один выбранный канал не проверил номер — не утверждаем, что мессенджера нет. Если каждый
            # непроверенный канал назвал срок (лимит, flood wait) — ждём ближайший, не тратя попытку
            waits = [w for w in unchecked_waits if w is not None]
            retry_at = min(waits) if waits and len(waits) == len(unchecked) else None
            raise OrbitaError(f"не найден: {', '.join(not_found) or '—'}; не проверен: {'; '.join(unchecked)}",
                              retry_at)
        await self._no_messenger(deal_id, "no_messenger", f"мессенджер не найден: {', '.join(phones)}")

    async def _start(self, deal_id: int, phone: str, channel: str, text: str, key: str,
                     name: str) -> FirstSendResult:
        """Один запрос «написать первым». Намерение (номер, канал, ключ) пишется в базу ДО запроса и
        стирается, только когда исход определён: ответ Орбиты или её отказ, по которому точно ничего
        не ушло. Сбой связи, 5xx, idempotency_in_progress оставляют намерение — повтор пойдёт туда же."""
        self.db.update(deal_id, intent_phone=phone, intent_channel=channel, intent_key=key)
        try:
            res = await self.orbita.start_dialog(channel, phone, text, key, name or None)
        except OrbitaAPIError as e:
            if e.definite:
                self.db.update(deal_id, intent_phone=None, intent_channel=None, intent_key=None)
            raise
        except OrbitaError:
            raise                                       # сбой связи: повтор с тем же ключом безопасен
        except Exception as e:                          # непредвиденный сбой посреди отправки
            raise UnknownOutcome(f"{phone}: {type(e).__name__}: {e}") from e
        self.db.update(deal_id, intent_phone=None, intent_channel=None, intent_key=None)
        return res

    async def _first_done(self, deal_id: int, phone: str, res: FirstSendResult) -> bool:
        """Сообщение ушло (или могло уйти, 202) — записать и закончить перебор."""
        if res.status == "sent":
            self._mark_sent_first(deal_id, phone, res.dialog_id, res.channel)
            await self.note(deal_id, f"Написал клиенту первым ({res.channel or 'мессенджер'}, {phone}).")
            return True
        if res.status == "pending":
            self.db.update(deal_id, status="pending_first", phone=phone, channel=res.channel,
                           request_key=res.request_key, last_error=None)
            self.event(deal_id, "первое сообщение передано, ждём подтверждения",
                       f"{phone} {res.channel} {res.request_key}")
            return True
        return False

    def _too_old(self, deal_id: int, row, created: datetime | None) -> bool:
        start = max(filter(None, (created, _ts(row["retried_at"]))), default=None)
        if start is None or datetime.now(timezone.utc) - start <= MAX_AGE:
            return False
        self.db.update(deal_id, status="skipped", last_error="заявка старше 48 ч")
        self.event(deal_id, "пропущена", "заявка старше 48 ч")
        return True

    def _mark_sent_first(self, deal_id: int, phone: str | None, dialog_id: str | None,
                         channel: str | None) -> None:
        due = datetime.now(timezone.utc) + timedelta(hours=self.s.followup_hours)
        fields = dict(status="sent_first", dialog_id=dialog_id, channel=channel,
                      first_sent_at=datetime.now(timezone.utc).isoformat(),
                      followup_due_at=due.isoformat(timespec="seconds"), last_error=None)
        if phone:
            fields["phone"] = phone
        self.db.update(deal_id, **fields)
        self.event(deal_id, "первое сообщение отправлено", f"{phone or ''} {channel or ''}")

    async def _no_messenger(self, deal_id: int, status: str, reason: str) -> None:
        if not self.dry_run:
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

        replied = client_replied(await self.orbita.history(row["dialog_id"]), _ts(row["first_sent_at"]))
        if replied is None:
            self.db.update(deal_id, status="needs_check", last_error="не смог прочитать историю диалога")
            self.event(deal_id, "не понял, ответил ли клиент")
            await self.note(deal_id, "Не смог проверить, ответил ли клиент. Напоминание не отправлял.")
            return
        if replied:
            await self._client_replied(deal_id, "по истории")
            return
        if not self.s.followup_on:
            self.db.update(deal_id, status="no_reply")
            self.event(deal_id, "клиент не ответил", "напоминание выключено")
            await self.note(deal_id, f"Клиент не ответил за {self.s.followup_hours:g} ч. "
                                     "Напоминание выключено — дальше вручную.")
            return

        text = self.render("followup", row["name"] or "", deal.title)
        key = f"deal-{deal_id}-followup" + (f"-r{row['followup_retry_n']}" if row["followup_retry_n"] else "")
        try:
            res = await self.orbita.send_reply(row["dialog_id"], text, key)
        except OrbitaError:
            raise
        except Exception as e:
            raise UnknownOutcome(f"напоминание: {type(e).__name__}: {e}") from e
        pending = res.status == "pending"
        self.db.update(deal_id, status="sent_followup", followup_request_key=res.request_key if pending else None,
                       followup_sent_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if pending:
            self.event(deal_id, "напоминание передано, ждём подтверждения", res.request_key or "")
            await self.note(deal_id, f"Клиент не ответил за {self.s.followup_hours:g} ч — передал напоминание "
                                     "в мессенджер.")
        else:
            self.event(deal_id, "напоминание отправлено")
            await self.note(deal_id, f"Клиент не ответил за {self.s.followup_hours:g} ч — отправил напоминание.")

    async def after_followup(self, deal_id: int) -> None:
        """Через followup_hours после напоминания — итог: ответил или нет. Иначе статус висит вечно."""
        row = self.db.deal(deal_id)
        if row is None or row["status"] != "sent_followup" or not row["followup_sent_at"]:
            return
        if datetime.now(timezone.utc) < _ts(row["followup_sent_at"]) + timedelta(hours=self.s.followup_hours):
            return
        replied = client_replied(await self.orbita.history(row["dialog_id"]), _ts(row["first_sent_at"])) \
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
        row = self.db.deal(deal_id)
        fields: dict = {"status": "replied", "client_replied_at": now()}
        if self.s.reply_on and not row["reply_sent_at"] and not self.dry_run:
            due = datetime.now(timezone.utc) + timedelta(minutes=self.s.reply_delay_minutes)
            fields["reply_due_at"] = due.isoformat(timespec="seconds")
        self.db.update(deal_id, **fields)
        self.event(deal_id, f"клиент ответил ({how})")
        await self.note(deal_id, "Клиент ответил в мессенджере.")

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
            replied = client_replied(await self.orbita.history(row["dialog_id"]), sent)
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
        history = await self.orbita.history(row["dialog_id"])
        client_msg = first_incoming(history, _ts(row["first_sent_at"]))
        if client_msg is None:
            self.db.update(deal_id, reply_due_at=None)
            self.event(deal_id, "ответ по шаблону не нужен", "не нашёл сообщение клиента в истории")
            return
        if answered_after(history, client_msg.at):
            self.db.update(deal_id, reply_due_at=None)
            self.event(deal_id, "ответ по шаблону не нужен", "менеджер уже ответил")
            return
        if datetime.now(timezone.utc) - client_msg.at > REPLY_MAX_LATE:
            self.db.update(deal_id, reply_due_at=None)
            self.event(deal_id, "ответ по шаблону не отправлен", "клиент написал больше 30 мин назад")
            return
        text = self.render("reply", row["name"] or "")
        key = f"deal-{deal_id}-reply" + (f"-r{row['reply_retry_n']}" if row["reply_retry_n"] else "")
        try:
            res = await self.orbita.send_reply(row["dialog_id"], text, key)
        except OrbitaError:
            raise
        except Exception as e:
            raise UnknownOutcome(f"ответ по шаблону: {type(e).__name__}: {e}") from e
        pending = res.status == "pending"
        self.db.update(deal_id, reply_sent_at=now(), reply_due_at=None, attempts=0,
                       reply_request_key=res.request_key if pending else None)
        if pending:
            self.event(deal_id, "ответ по шаблону передан, ждём подтверждения", res.request_key or "")
            await self.note(deal_id, "Передал клиенту ответ по шаблону. Дальше — менеджер.")
        else:
            self.event(deal_id, "ответ по шаблону отправлен")
            await self.note(deal_id, "Ответил клиенту по шаблону. Дальше — менеджер.")

    # ---------- вебхуки Орбиты ----------

    async def process_inbox(self) -> None:
        """Разобрать сохранённые события Орбиты (таблица orbita_events, state='new').

        Событие помечается обработанным только после успешного on_orbita_event; сбой оставляет его в
        очереди до следующего прохода, после MAX_ATTEMPTS сбоев — failed. Отмена (остановка сервиса)
        оставляет событие в очереди: после перезапуска его подхватит первый проход. Обработка
        идемпотентна — каждое действие проверяет статус сделки."""
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
                if row and row["status"] in ("sent_first", "sent_followup", "no_reply"):
                    await self._client_replied(row["deal_id"], "вебхук")
            elif kind in ("message.sent", "message.failed") and data.get("request_key"):
                await self._send_outcome(kind == "message.sent", data)
            elif kind in ("channel.disconnected", "channel.connected"):
                self.event(None, f"Орбита: {kind}", str(data.get("provider") or data.get("id") or ""))


    async def _send_outcome(self, ok: bool, data: dict) -> None:
        """Исход отправки, на которую Орбита ответила 202: первое сообщение, напоминание или ответ по шаблону."""
        rk = data["request_key"]
        reason = str(data.get("reason_code") or "причина не указана")
        row = self.db.deal_by("request_key", rk)
        if row and row["status"] == "pending_first":
            deal_id = row["deal_id"]
            if ok:
                dialog = data.get("dialog_id")
                self._mark_sent_first(deal_id, None, str(dialog) if dialog is not None else None, row["channel"])
                await self.note(deal_id, f"Написал клиенту первым ({row['channel']}, {row['phone']}).")
            else:
                self.db.update(deal_id, status="needs_check", last_error=f"не доставлено: {reason}")
                self.event(deal_id, "первое сообщение не доставлено", reason)
                await self.note(deal_id, f"Первое сообщение не доставлено ({reason}). Нужен звонок менеджера.")
            return
        for step, column, title in (("followup", "followup_request_key", "напоминание"),
                                    ("reply", "reply_request_key", "ответ по шаблону")):
            row = self.db.deal_by(column, rk)
            if row is None:
                continue
            deal_id = row["deal_id"]
            if ok:
                self.db.update(deal_id, **{column: None})
                self.event(deal_id, f"{title}: мессенджер подтвердил доставку")
                return
            fields: dict = {column: None}
            # Сообщение не ушло: шаг не сделан. Человек решает, повторять ли («Повторить» — с новым ключом)
            expected = "sent_followup" if step == "followup" else "replied"
            if row["status"] == expected:
                fields.update(status="needs_check", last_error=f"{title} не доставлен(о): {reason}")
                if step == "followup":
                    fields["followup_sent_at"] = None
                else:
                    fields.update(reply_sent_at=None, reply_due_at=now())   # ответ всё ещё нужен
            self.db.update(deal_id, **fields)
            self.event(deal_id, f"{title} не доставлен(о)", reason)
            if row["status"] == expected:
                await self.note(deal_id, f"Не удалось доставить {title} ({reason}). Проверьте переписку; "
                                         "«Повторить» в панели робота отправит заново.")
            return


def render(tpl: str, name: str, deal_title: str = "") -> str:
    """Подстановка {name} и {deal_title}. Ошибка в шаблоне — KeyError / ValueError / IndexError."""
    return tpl.strip().format(name=name, deal_title=deal_title)
