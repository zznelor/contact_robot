"""Общие фикстуры и подделки Битрикса и Орбиты. Без сети, у каждого теста своя база в tmp_path."""

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from first_contact import bitrix as bitrix_mod
from first_contact import config, ui_settings
from first_contact import orbita_api as orbita_api_mod
from first_contact.orbita_api import OrbitaAPIError
from first_contact import worker as worker_mod
from first_contact.app import create_app
from first_contact.auth import SESSION_COOKIE, Users
from first_contact.bitrix import BitrixError, Contact, Deal
from first_contact.db import DB
from first_contact.orbita import FirstSendResult, OrbitaError, ReplyResult
from first_contact.worker import Worker

UTC = timezone.utc
ORIGIN = {"Origin": "http://testserver"}
PHONE = "+79990000001"

# Всё, что config.env() и config._seed_from_env() читают из окружения
ENV_VARS = ("DB_PATH", "PUBLIC_URL", "DOMAIN", "PORT", "POLL_SECONDS", "DRY_RUN", "HOST", "FORWARDED_ALLOW_IPS",
            "BITRIX_WEBHOOK", "BITRIX_APP_TOKEN", "NO_MESSENGER_STAGE", "DEAL_CATEGORY_ID", "ORBITA_API_URL",
            "ORBITA_API_KEY", "ORBITA_WEBHOOK_SECRET", "ORBITA_CHANNELS", "TZ_NAME", "FOLLOWUP_HOURS", "SEND_WINDOW")


def ago(**kw) -> datetime:
    return datetime.now(UTC) - timedelta(**kw)


def run(coro):
    return asyncio.run(coro)


def midday_tz() -> str:
    """Часовой пояс, в котором сейчас 12:xx: окно 06:00–18:00 в нём заведомо открыто, далеко от границ."""
    offset = 12 - datetime.now(UTC).hour            # -11 … +12
    return f"Etc/GMT{-offset:+d}"                   # у Etc/GMT знак обратный: Etc/GMT-3 — это UTC+3


def wait_for(cond, timeout: float = 5.0) -> None:
    """Опрос состояния с таймаутом — для фоновых задач вебхука, которые идут в цикле TestClient."""
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("условие не выполнилось за отведённое время")
        time.sleep(0.01)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """Подпись по формуле из docs/РАЗРАБОТЧИКУ.md: v1=HMAC-SHA256(секрет, "<timestamp>.<тело>")."""
    return "v1=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


# ---------- изоляция окружения ----------

@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Ни ../.env, ни templates.toml, ни переменные окружения машины в тест не попадают."""
    monkeypatch.setattr(config, "ENV_FILES", [])
    monkeypatch.setattr(config, "ROOT", tmp_path)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "state.sqlite3"))


@pytest.fixture
def db():
    d = DB(config.env().db_path)
    yield d
    d.conn.close()


# ---------- подделки для логики робота ----------

class FakeBitrix:
    """Битрикс: одна воронка, этап и один контакт на все сделки; запоминает комментарии и смены этапа."""

    def __init__(self):
        self.notes: list[tuple[int, str]] = []
        self.stages_set: list[tuple[int, str]] = []
        self.max_id = 100
        self.new_ids: list[int] = []
        self.category, self.stage, self.semantic = "0", "NEW", "P"
        self.name, self.phones = "Анна", [PHONE]
        self.created: datetime | None = None          # DATE_CREATE сделки; None — Битрикс его не отдал

    async def max_deal_id(self):
        return self.max_id

    async def deal_ids_after(self, last, category_id):
        return [i for i in self.new_ids if i > last]   # фильтр >ID, как в crm.deal.list

    async def get_deal(self, deal_id):
        return Deal(deal_id, "Заявка", self.stage, self.semantic, self.category, self.created)

    async def deal_contacts(self, deal_id):
        return [Contact(1, self.name, list(self.phones))]

    async def set_stage(self, deal_id, stage_id):
        self.stages_set.append((deal_id, stage_id))
        self.stage = stage_id

    async def comment(self, deal_id, text):
        self.notes.append((deal_id, text))

    def noted(self, deal_id) -> bool:
        return any(d == deal_id for d, _ in self.notes)


class FakeOrbita:
    """Орбита: mode — sent | none (номера нигде нет) | boom (Орбита отказала, 503).
    connected — подключённые каналы (по умолчанию все); answers[канал] — исход в этом канале вместо mode
    (ещё pending — 202, reused — 422 idempotency_key_reused, lost — Орбита приняла запрос, а ответ до робота
    не дошёл)."""

    def __init__(self):
        self.mode = "sent"
        self.reply_mode = "sent"
        self.connected = set(config.CHANNELS)
        self.answers: dict[str, str] = {}
        self.first: list[tuple[str, str, str]] = []     # (телефон, текст, ключ) — ушедшие первые
        self.keys: list[str] = []                        # ключи всех попыток «написать первым»
        self.replies: list[tuple[str, str, str]] = []    # (диалог, текст, ключ)
        self.history_items: dict = {}
        self.history_calls: list[str] = []

    async def connected_channels(self):
        return set(self.connected)

    async def start_dialog(self, channel, phone, text, key, name=None):
        self.keys.append(key)
        how = self.answers.get(channel) or {"sent": "sent", "none": "not_found", "boom": "boom"}[self.mode]
        if how == "boom":
            raise OrbitaError("HTTP 503: Орбита недоступна")
        if how == "reused":
            raise OrbitaAPIError(422, "idempotency_key_reused", {"code": "idempotency_key_reused"})
        if how in ("not_found", "unchecked", "invalid_phone"):
            return FirstSendResult(how, channel=channel, raw=how)
        self.first.append((phone, text, key))
        if how == "lost":
            raise OrbitaError("POST /dialogs: ReadTimeout: ответ не дошёл")
        if how == "pending":
            return FirstSendResult("pending", None, channel, request_key=f"rk-{key}")
        return FirstSendResult("sent", "D1", channel)

    async def send_reply(self, dialog_id, text, key):
        """reply_mode: sent — 201 (Telegram подтвердил); pending — 202, исход придёт вебхуком с request_key;
        lost — Орбита приняла запрос, а ответ до робота не дошёл (обрыв связи); reused — 422
        idempotency_key_reused: этот ключ уже приходил с другим телом."""
        self.replies.append((dialog_id, text, key))
        if self.reply_mode == "lost":
            raise OrbitaError("POST /messages: ReadTimeout: ответ не дошёл")
        if self.reply_mode == "reused":
            raise OrbitaAPIError(422, "idempotency_key_reused", {"code": "idempotency_key_reused"})
        if self.reply_mode == "pending":
            return ReplyResult("pending", f"rk-{key}")
        return ReplyResult("sent")

    async def history(self, dialog_id, since=None):
        self.history_calls.append(dialog_id)
        return self.history_items.get(dialog_id, [])


@pytest.fixture
def bitrix():
    return FakeBitrix()


@pytest.fixture
def orbita():
    return FakeOrbita()


@pytest.fixture
def settings(db):
    s = config.load(db)
    s.bitrix_webhook, s.no_messenger_stage, s.orbita_api_key = "https://b.bitrix24.ru/rest/1/x/", "NO_MESSENGER", "k"
    s.window_start, s.window_end, s.tz_name = "06:00", "18:00", midday_tz()
    return s


@pytest.fixture
def worker(db, settings, bitrix, orbita):
    return Worker(config.env(), settings, db, bitrix, orbita)


@pytest.fixture
def running(worker, bitrix):
    """Включённый робот: сделки до ID 100 не трогает."""
    run(worker.enable())
    return worker


class WatchedLock(asyncio.Lock):
    """Замок робота, который сообщает, что кто-то встал в очередь за ним (contended)."""

    def __init__(self):
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.contended.set()
        return await super().acquire()


class Gate:
    """Подмена асинхронного метода подделки: вызов останавливается, пока тест не откроет ворота."""

    def __init__(self, obj, name: str):
        self.entered, self.opened = asyncio.Event(), asyncio.Event()
        original = getattr(obj, name)

        async def gated(*a, **kw):
            self.entered.set()
            await self.opened.wait()
            return await original(*a, **kw)
        setattr(obj, name, gated)


async def first_of(*aws) -> None:
    """Дождаться первого из: завершения задачи / события."""
    tasks = [a if isinstance(a, asyncio.Task) else asyncio.ensure_future(a) for a in aws]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)


def async_panel(w: Worker, role: str = "admin"):
    """Клиент панели в том же цикле событий, что и робот (для одновременных действий)."""
    users = Users(w.db)
    uid = users.create(f"{role}@x.ru", "password1", role)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(w)), base_url="http://testserver",
                             headers=ORIGIN, cookies={SESSION_COOKIE: users.new_session(uid)})


class Clock:
    """Часы робота (worker.datetime) и клиента Битрикса (bitrix.datetime — паузы после ошибок лимитов), которые можно
    перевести вперёд: паузы между попытками без ожидания."""

    def __init__(self):
        self.shift = timedelta(0)

    def advance(self, **kw) -> None:
        self.shift += timedelta(**kw)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()

    class Shifted(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + c.shift
    monkeypatch.setattr(worker_mod, "datetime", Shifted)
    monkeypatch.setattr(bitrix_mod, "datetime", Shifted)
    return c


@contextmanager
def status_write_fails(db: DB, when: str):
    """База отказывает в записи статуса сделки по условию when (SQL над NEW.*) — как обрыв процесса или сбой
    диска посреди перехода. Временный триггер SQLite, в файл базы не попадает."""
    db.conn.execute(f"CREATE TEMP TRIGGER status_write_fails BEFORE UPDATE OF status ON deals WHEN {when} "
                    "BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END")
    try:
        yield
    finally:
        db.conn.execute("DROP TRIGGER status_write_fails")


def start_deal(w: Worker, bitrix: FakeBitrix, deal_id: int):
    """Новая сделка из Битрикса → проход робота. Возвращает строку сделки из базы."""
    bitrix.new_ids.append(deal_id)
    run(w.tick())
    return w.db.deal(deal_id)


# ---------- настоящий клиент Орбиты на подменённом транспорте ----------

@pytest.fixture
def http(monkeypatch):
    """Подменяет httpx.AsyncClient в orbita_api; calls — все запросы: (метод, путь, Idempotency-Key, тело, X-Api-Key)."""
    calls = []
    real = httpx.AsyncClient

    def install(handler):
        def recording(req):
            body = json.loads(req.content) if req.content else None
            calls.append((req.method, req.url.path, req.headers.get("idempotency-key"), body,
                          req.headers.get("x-api-key")))
            return handler(req)

        class Client(real):
            def __init__(self, **kw):
                super().__init__(transport=httpx.MockTransport(recording), **kw)
        monkeypatch.setattr(orbita_api_mod.httpx, "AsyncClient", Client)
        return calls
    return install



# ---------- приложение: вебхуки ----------

@pytest.fixture
def webhook_app(worker):
    worker.s.orbita_webhook_secret = "whsec_t"
    return create_app(worker)


def post_orbita(client: TestClient, ev, *, secret: str = "whsec_t", event_id: str | None = None,
                timestamp: str | None = None):
    body = ev if isinstance(ev, bytes) else json.dumps(ev).encode()
    ts = timestamp or str(int(time.time()))
    headers = {"X-Orbita-Signature": sign(secret, ts, body), "X-Orbita-Timestamp": ts}
    if event_id is not None:
        headers["X-Orbita-Event-Id"] = event_id
    return client.post("/orbita/webhook", content=body, headers=headers)


# ---------- приложение: панель ----------

class PanelBitrix:
    """Битрикс для панели: адрес с «bad» не принимается, две воронки со своими этапами (stages — только этапы в
    работе, как Bitrix.stages)."""

    def __init__(self, webhook="", *a):
        self.webhook = webhook

    async def comment(self, *a):
        pass

    async def max_deal_id(self):
        if "bad" in self.webhook:
            raise RuntimeError("401 invalid_token")
        return 40

    async def deal_ids_after(self, *a):
        return []

    async def categories(self):
        return [("0", "Общая"), ("3", "Китай")]

    async def stages(self, category_id):
        if "nostages" in self.webhook:
            raise BitrixError("crm.status.list: ACCESS_DENIED")
        if category_id == "0":
            return [("NEW", "Новая"), ("NO_MESSENGER", "Не нашёл мессенджер")]
        return [("C3:NEW", "Новая"), ("C3:LOST", "Не дозвонились")]

    async def get_deal(self, deal_id):
        raise RuntimeError("тестовый Битрикс")


class PanelOrbita:
    """Орбита для панели: ключ «bad» не принимается; номер на «1» есть в WhatsApp, остальные — нигде.
    sent — тестовые отправки этого клиента (worker.orbita.sent). Не атрибут класса: модуль теста, импортированный
    в прошлой сессии pytest того же процесса (так гоняет mutmut), держал бы класс прошлого conftest."""

    def __init__(self, url="", key="", channels=()):
        self.key = key
        self.sent: list[tuple[str, str, str]] = []

    async def account(self):
        if self.key == "bad":
            raise RuntimeError("HTTP 401 invalid_key")
        return {"name": "Тест", "subscription_active": True}

    async def channels(self):
        return [{"канал": "whatsapp", "подключён": True}, {"канал": "telegram", "подключён": False}]

    async def channel_status(self):
        return {"whatsapp": True, "max": None, "telegram": False}

    async def send_first(self, phone, text, key, name=None):
        self.sent.append((phone, text, key))
        return FirstSendResult("sent", "9", "whatsapp") if phone.endswith("1") else FirstSendResult("no_messenger")


@pytest.fixture
def panel(db, monkeypatch):
    monkeypatch.setattr(ui_settings, "Bitrix", PanelBitrix)
    monkeypatch.setattr(ui_settings, "OrbitaAPI", PanelOrbita)
    monkeypatch.setattr(worker_mod, "Bitrix", PanelBitrix)
    monkeypatch.setattr(worker_mod, "OrbitaAPI", PanelOrbita)
    s = config.load(db)
    w = Worker(config.env(), s, db)
    app = create_app(w)
    return SimpleNamespace(worker=w, s=s, db=db, app=app, users=Users(db))


def login_client(panel, email: str, role: str, password: str = "password1") -> TestClient:
    """Клиент с уже открытой сессией пользователя (вход по форме проверяется отдельно)."""
    uid = panel.users.create(email, password, role)
    c = TestClient(panel.app, headers=ORIGIN)
    c.cookies.set(SESSION_COOKIE, panel.users.new_session(uid))
    return c


@pytest.fixture
def admin(panel) -> TestClient:
    return login_client(panel, "a@x.ru", "admin")


@pytest.fixture
def configured(panel):
    """Битрикс с этапом и Орбита подключены (через worker.save, как это делает панель)."""
    panel.worker.save(bitrix_webhook="https://p.bitrix24.ru/rest/1/good/", no_messenger_stage="NO_MESSENGER",
                      orbita_api_key="orb_pk_secret123456", orbita_channels=["whatsapp"])
    return panel
