"""Проверки клиента REST API Орбиты и вебхуков на моках по спецификации (без сети).

Запуск: uv run python test_orbita_api.py
"""
import asyncio, hashlib, hmac, json, sys, tempfile, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
HERE = __import__("pathlib").Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import httpx
from first_contact import orbita_api
from first_contact.orbita_api import OrbitaAPI, verify_signature
from first_contact.orbita import OrbitaError, client_replied

ok = 0
def check(cond, name):
    global ok
    assert cond, name; ok += 1; print("✔", name)

# 1. подпись — пример из спецификации
spec = json.load(open(HERE / "docs" / "orbita-openapi-2026-10-20.json"))["info"]["x-orbita-signature-example"]
check(verify_signature(spec["secret"], spec["timestamp"], spec["body"].encode(), spec["signature"], max_skew=0), "подпись примера из спецификации")
check(not verify_signature(spec["secret"], spec["timestamp"], spec["body"].encode()+b" ", spec["signature"], max_skew=0), "подделанное тело отвергнуто")
check(not verify_signature(spec["secret"], spec["timestamp"], spec["body"].encode(), spec["signature"]), "старый timestamp отвергнут")

# 2. мок API
REAL = httpx.AsyncClient
def mock(handler):
    real = REAL
    class C(real):
        def __init__(self, **kw): super().__init__(transport=httpx.MockTransport(handler), **kw)
    orbita_api.httpx.AsyncClient = C

calls = []
def make(responses, channels=(("whatsapp", True), ("telegram", False), ("max", True))):
    def h(req):
        calls.append((req.method, req.url.path, req.headers.get("idempotency-key"), req.content and json.loads(req.content)))
        assert req.headers["x-api-key"] == "k"
        if req.url.path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{"provider": p, "is_connected": c, "id": i} for i, (p, c) in enumerate(channels)]})
        if req.url.path.endswith("/dialogs") and req.method == "POST":
            ch = json.loads(req.content)["channel"]
            st, body = responses[ch]
            return httpx.Response(st, json=body)
        if req.url.path.endswith("/messages") and req.method == "GET":
            return httpx.Response(200, json={"items": [
                {"direction": "incoming", "sent_at": "2026-10-20T10:10:00+00:00"},
                {"direction": "outgoing", "sent_at": "2026-10-20T10:00:00+00:00"}]})
        if req.url.path.endswith("/messages") and req.method == "POST":
            return httpx.Response(202, json={"request_key": "rk", "message_id": 5})
        return httpx.Response(404, json={"detail": {"code": "not_found"}})
    return h

api = OrbitaAPI("https://x/api/public/v1", "k", ["whatsapp", "max", "telegram"])
run = asyncio.run
nf = lambda c: (422, {"detail": {"code": c}})

mock(make({"whatsapp": (201, {"dialog": {"id": 501}, "message_id": 1})}))
r = run(api.send_first("+79990000001", "привет", "deal-1-first-+7999", "Анна"))
check(r.status == "sent" and r.dialog_id == "501" and r.channel == "whatsapp", "201 → отправлено, диалог 501")
check(calls[-1][2] == "deal-1-first-+7999-whatsapp" and calls[-1][3]["name"] == "Анна", "ключ идемпотентности по каналу, имя передано")

calls.clear()
mock(make({"whatsapp": nf("not_on_whatsapp"), "max": (201, {"dialog": {"id": 7}})}))
r = run(api.send_first("+7", "t", "k1"))
check(r.status == "sent" and r.channel == "max", "нет в WhatsApp → написали в Max")
check(not any(c[3] and c[3].get("channel") == "telegram" for c in calls), "telegram не подключён — не трогаем")

calls.clear()
api_max_first = OrbitaAPI("https://x/api/public/v1", "k", ["max", "whatsapp"])
mock(make({"max": nf("max_not_found"), "whatsapp": (201, {"dialog": {"id": 8}})}))
r = run(api_max_first.send_first("+7", "t", "k-order"))
tried = [c[3]["channel"] for c in calls if c[1].endswith("/dialogs")]
check(tried == ["max", "whatsapp"] and r.channel == "whatsapp", "порядок из настроек: сначала Max, потом WhatsApp")

mock(make({"whatsapp": nf("not_on_whatsapp"), "max": nf("max_not_found")}))
check(run(api.send_first("+7", "t", "k2")).status == "no_messenger", "нигде не найден → no_messenger")

mock(make({"whatsapp": nf("not_connected"), "max": nf("max_not_found")}))
try: run(api.send_first("+7", "t", "k3")); check(False, "x")
except OrbitaError as e: check("не проверен" in str(e), "WhatsApp лежит → ошибка, а не «не найден»")

mock(make({"whatsapp": nf("daily_send_limit_exhausted")}))
try: run(api.send_first("+7", "t", "k4")); check(False, "x")
except OrbitaError as e: check("daily_send_limit" in str(e), "суточный лимит ключа → ошибка (повтор позже)")

mock(make({}, channels=[("whatsapp", False)]))
try: run(api.send_first("+7", "t", "k5")); check(False, "x")
except OrbitaError as e: check("нет подключённых" in str(e), "нет каналов → ошибка, а не «не найден»")

mock(make({}, channels=[("whatsapp", False), ("whatsapp", True), ("max", False)]))
check(run(api.channel_status()) == {"whatsapp": True, "max": False, "telegram": None},
      "статус каналов: все три, даже не заведённый Telegram; два подключения WhatsApp — живо хоть одно")

mock(make({"whatsapp": (202, {"request_key": "rk-1"})}))
r = run(api.send_first("+7", "t", "k6"))
check(r.status == "pending" and r.request_key == "rk-1", "202 → pending с request_key")

def boom(req): raise httpx.ConnectTimeout("t")
mock(boom)
try: run(api.send_first("+7", "t", "k7")); check(False, "x")
except OrbitaError: check(True, "обрыв связи → повторяемая ошибка (идемпотентно)")

mock(make({"whatsapp": (201, {"message_id": 1})}))
r = run(api.send_first("+7", "t", "k-nodialog"))
check(r.status == "sent" and r.dialog_id is None, "201 без диалога → номер диалога пустой, а не «None»")
mock(lambda req: httpx.Response(502, json=["bad gateway"]))
try: run(api.account()); check(False, "x")
except OrbitaError as e: check("502" in str(e), "ошибка не в виде объекта — понятное исключение")

mock(make({}))
msgs = run(api.history("501"))
check([m.outgoing for m in msgs] == [False, True], "история: направления")
check(client_replied(msgs, datetime(2026,10,20,10,0,tzinfo=timezone.utc)) is True, "клиент ответил после нашего")
check(client_replied(msgs, datetime(2026,10,20,10,30,tzinfo=timezone.utc)) is None, "старая реплика клиента не считается ответом")
check(run(api.send_reply("501", "второе", "k8"))["request_key"] == "rk", "send_reply 202 — ок")

# 3. воркер + HTTP вебхук
import os
tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = f"{tmp}/s.sqlite3"
from first_contact import config
from first_contact.db import DB
from first_contact.worker import Worker
from first_contact.app import create_app
from fastapi.testclient import TestClient

class FakeBitrix:
    notes = []
    async def comment(self, d, t): self.notes.append((d, t))
env = config.env()
db = DB(env.db_path)
s = config.load(db)
s.orbita_webhook_secret = "whsec_t"
w = Worker(env, s, db, FakeBitrix(), api)
db.add_deal(10); db.update(10, status="sent_first", dialog_id="501", phone="+7")
db.add_deal(11); db.update(11, status="pending_first", request_key="rk-9", channel="whatsapp", phone="+8")
db.add_deal(12); db.update(12, status="pending_first", request_key="rk-10", channel="max", phone="+9")

app = create_app(w)
client = TestClient(app)  # без with — фоновый цикл не стартует
def send(ev, secret="whsec_t", eid=None):
    body = json.dumps(ev).encode(); ts = str(int(time.time()))
    sig = "v1=" + hmac.new(secret.encode(), ts.encode()+b"."+body, hashlib.sha256).hexdigest()
    r = client.post("/orbita/webhook", content=body, headers={"X-Orbita-Signature": sig, "X-Orbita-Timestamp": ts, "X-Orbita-Event-Id": eid or str(ev["id"])})
    time.sleep(0.2); return r
check(send({"id": 1, "type": "message.received", "data": {"dialog_id": 501, "direction": "incoming"}}, secret="bad").status_code == 401, "вебхук с чужой подписью → 401")
check(send({"id": 2, "type": "message.received", "data": {"dialog_id": 501, "direction": "incoming"}}).json()["ok"], "вебхук принят")
check(db.deal(10)["status"] == "replied", "message.received → клиент ответил")
check(send({"id": 2, "type": "message.received", "data": {}}).json().get("duplicate"), "повтор события отсечён")
send({"id": 3, "type": "message.sent", "data": {"request_key": "rk-9", "dialog_id": 777, "direction": "outgoing"}})
check(db.deal(11)["status"] == "sent_first" and db.deal(11)["dialog_id"] == "777" and db.deal(11)["followup_due_at"], "message.sent → sent_first, диалог 777, второе запланировано")
send({"id": 4, "type": "message.failed", "data": {"request_key": "rk-10", "reason_code": "send_failed"}})
check(db.deal(12)["status"] == "needs_check", "message.failed → проверить вручную")
check(len(FakeBitrix.notes) == 3, "комментарии в сделки: 3")
db.update(10, status="sent_first")
def send_noid(ev):
    body = json.dumps(ev).encode(); ts = str(int(time.time()))
    sig = "v1=" + hmac.new(b"whsec_t", ts.encode()+b"."+body, hashlib.sha256).hexdigest()
    r = client.post("/orbita/webhook", content=body, headers={"X-Orbita-Signature": sig, "X-Orbita-Timestamp": ts})
    time.sleep(0.2); return r
send_noid({"type": "channel.connected", "data": {"provider": "max"}})
check(not send_noid({"type": "message.received", "data": {"dialog_id": 501, "direction": "incoming"}}).json().get("duplicate")
      and db.deal(10)["status"] == "replied", "события без номера не считаются дублями друг друга")
orig = w.on_orbita_event
async def broken(ev): raise RuntimeError("сбой")
w.on_orbita_event = broken
send({"id": 50, "type": "message.received", "data": {}})
check(any(e["event"] == "Орбита: ошибка обработки события" for e in db.events()), "сбой обработки вебхука — в журнале")
w.on_orbita_event = orig
check(not send({"id": 50, "type": "message.received", "data": {}}).json().get("duplicate"),
      "после сбоя повторная доставка обрабатывается")
print(f"\nВсе проверки пройдены: {ok}")
