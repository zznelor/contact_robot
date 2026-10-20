"""Логика робота на подделках Битрикса и Орбиты (без сети).

Запуск: uv run python test_worker.py
"""
import asyncio, os, sys, tempfile
from datetime import datetime, timedelta, timezone
HERE = __import__("pathlib").Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["DB_PATH"] = f"{tempfile.mkdtemp()}/w.sqlite3"
from first_contact import config
from first_contact.bitrix import Deal
from first_contact.db import DB
from first_contact.orbita import FirstSendResult, Message
from first_contact.worker import Worker

ok = 0
def check(cond, name):
    global ok
    assert cond, name; ok += 1; print("✔", name)

UTC = timezone.utc
def ago(**kw): return datetime.now(UTC) - timedelta(**kw)

class FakeBitrix:
    def __init__(self): self.notes, self.stages_set, self.max_id, self.new_ids, self.category = [], [], 100, [], "0"
    async def max_deal_id(self): return self.max_id
    async def deal_ids_after(self, last, cat): return [i for i in self.new_ids if i > last]
    async def get_deal(self, i): return Deal(i, "Заявка", "NEW", "P", self.category, 1)
    async def get_contact(self, c): return "Анна", ["+79990000001"]
    async def set_stage(self, d, st): self.stages_set.append((d, st))
    async def comment(self, d, t): self.notes.append((d, t))

class FakeOrbita:
    def __init__(self): self.sent, self.history_items = [], {}
    async def send_first(self, phone, text, key, name=None):
        self.sent.append(("first", phone, text)); return FirstSendResult("sent", "D1", "whatsapp")
    async def send_reply(self, dialog, text, key):
        self.sent.append(("reply", dialog, text, key)); return {}
    async def history(self, dialog): return self.history_items.get(dialog, [])

env = config.env()
env.dry_run = False
db = DB(env.db_path)
s = config.load(db)
s.bitrix_webhook, s.no_messenger_stage, s.orbita_api_key = "https://b/rest/1/x/", "NO_MESSENGER", "k"
s.window_start, s.window_end = "00:00", "23:59"
bx, orb = FakeBitrix(), FakeOrbita()
w = Worker(env, s, db, bx, orb)
run = asyncio.run

# 1. выключенный робот ничего не делает
bx.new_ids = [101]
run(w.tick())
check(db.get("last_deal_id") is None and not orb.sent, "робот выключен — ничего не делает")
run(w.on_deal_added(101))
check(db.deal(101) is None, "событие Битрикса при выключенном роботе игнорируется")

# 2. включение: старые сделки не трогаем
bx.max_id = 101
run(w.enable())
check(s.enabled and db.get("last_deal_id") == "101", "включение запоминает последнюю сделку")
check(config.load(db).enabled, "флаг «включён» сохранён в базе")
bx.new_ids = [101, 102]
run(w.tick())
check(db.deal(101) is None and db.deal(102)["status"] == "sent_first", "сделка до включения не тронута, новая — написали")
check(orb.sent[-1][2].startswith("Анна"), "имя подставлено в первое сообщение")

# 3. клиент ответил → ответ по шаблону после паузы
s.reply_on, s.reply_delay_minutes = True, 0
first_at = datetime.fromisoformat(db.deal(102)["first_sent_at"])
orb.history_items["D1"] = [Message("", "", first_at, True), Message("", "", datetime.now(UTC), False)]
run(w._client_replied(102, "тест"))
check(db.deal(102)["status"] == "replied" and db.deal(102)["reply_due_at"], "ответ клиента → ответ по шаблону запланирован")
run(w.tick())
check(orb.sent[-1][0] == "reply" and orb.sent[-1][3] == "deal-102-reply", "ответ по шаблону отправлен")
check(db.deal(102)["reply_sent_at"] and not db.deal(102)["reply_due_at"], "отмечен как отправленный")
n = len(orb.sent); run(w._client_replied(102, "ещё раз")); run(w.tick())
check(len(orb.sent) == n, "второй раз ответ по шаблону не уходит")

# 4. менеджер ответил сам за время паузы → шаблон не нужен
bx.new_ids = [103]; run(w.tick())
first_at = datetime.fromisoformat(db.deal(103)["first_sent_at"])
client_at = datetime.now(UTC)
orb.history_items["D1"] = [Message("", "", first_at, True), Message("", "", client_at, False),
                           Message("", "", client_at + timedelta(seconds=30), True)]
run(w._client_replied(103, "тест")); n = len(orb.sent); run(w.tick())
check(len(orb.sent) == n and not db.deal(103)["reply_due_at"], "менеджер ответил сам — шаблон не отправлен")

# 5. клиент написал давно → шаблон не отправляем
bx.new_ids = [104]; run(w.tick())
db.update(104, first_sent_at=ago(hours=3).isoformat())
orb.history_items["D1"] = [Message("", "", ago(hours=3), True), Message("", "", ago(hours=2), False)]
run(w._client_replied(104, "тест")); n = len(orb.sent); run(w.tick())
check(len(orb.sent) == n, "клиент написал больше 30 мин назад — шаблон не отправлен")

# 6. ответ по шаблону выключен
s.reply_on = False
bx.new_ids = [105]; run(w.tick())
run(w._client_replied(105, "тест"))
check(not db.deal(105)["reply_due_at"], "ответ по шаблону выключен — не планируется")

# 7. напоминание выключено → статус «не ответил», ничего не шлём
s.followup_on = False
bx.new_ids = [106]; run(w.tick())
db.update(106, followup_due_at=ago(minutes=1).isoformat(), first_sent_at=ago(hours=25).isoformat())
orb.history_items["D1"] = [Message("", "", ago(hours=25), True)]
n = len(orb.sent); run(w.tick())
check(db.deal(106)["status"] == "no_reply" and len(orb.sent) == n, "напоминание выключено → «не ответил», без сообщения")

# 8. напоминание включено → уходит
s.followup_on = True
bx.new_ids = [107]; run(w.tick())
db.update(107, followup_due_at=ago(minutes=1).isoformat(), first_sent_at=ago(hours=25).isoformat())
run(w.tick())
check(db.deal(107)["status"] == "sent_followup" and orb.sent[-1][3] == "deal-107-followup", "напоминание отправлено")

# 9. опрос ответов без вебхуков
s.reply_on, s.orbita_webhook_secret = True, ""
bx.new_ids = [108]; run(w.tick())
first_at = datetime.fromisoformat(db.deal(108)["first_sent_at"])
orb.history_items["D1"] = [Message("", "", first_at, True), Message("", "", datetime.now(UTC), False)]
run(w.tick())
check(db.deal(108)["status"] == "sent_first", "опрос истории не чаще раза в 5 минут")
db.update(108, checked_at=ago(minutes=6).isoformat())
run(w.tick())
check(db.deal(108)["status"] == "replied", "без вебхуков ответ клиента найден опросом истории")

# 10. другая воронка
s.deal_category_id = "0"; bx.category = "5"
bx.new_ids = [109]; run(w.tick())
check(db.deal(109)["status"] == "skipped", "сделка другой воронки пропущена")
bx.category = "0"

# 10a. шаги по одному (кнопка в панели)
n = len(orb.sent)
bx.new_ids = [111]; run(w.run_step("poll"))
check(db.deal(111)["status"] == "new" and len(orb.sent) == n, "шаг «забрать сделки» только ставит в очередь")
run(w.run_step("first"))
check(db.deal(111)["status"] == "sent_first" and len(orb.sent) == n + 1, "шаг «написать первым» отправляет")
try: run(w.run_step("oops")); check(False, "x")
except KeyError: check(True, "неизвестный шаг не выполняется")

# 11. выключение
w.disable()
bx.new_ids = [110]; run(w.tick())
check(db.deal(110) is None and not config.load(db).enabled, "после выключения новые сделки не берёт")

print(f"\nВсе проверки пройдены: {ok}")
