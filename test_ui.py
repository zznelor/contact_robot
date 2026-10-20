"""Панель на временной базе с подделками Битрикса и Орбиты (без сети).

Запуск: uv run python test_ui.py [папка для HTML-снимков страниц]
"""
import os, re, sys, tempfile
HERE = __import__("pathlib").Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
tmp = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
os.makedirs(tmp, exist_ok=True)
os.environ["DB_PATH"] = f"{tmp}/ui.sqlite3"
if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])
for var in ("BITRIX_WEBHOOK", "ORBITA_API_KEY", "ORBITA_WEBHOOK_SECRET", "BITRIX_APP_TOKEN"):
    os.environ[var] = ""                   # не подтягивать настройки из ../.env
from first_contact import config, ui_settings
from first_contact.db import DB
from first_contact.orbita import FirstSendResult
from first_contact.worker import Worker
from first_contact.app import create_app
from fastapi.testclient import TestClient

ok = 0
def check(cond, name):
    global ok
    assert cond, name; ok += 1; print("✔", name)

class FakeBitrix:
    def __init__(self, webhook="", *a): self.webhook = webhook
    async def comment(self, *a): pass
    async def call(self, *a, **k): return []
    async def max_deal_id(self):
        if "bad" in self.webhook: raise RuntimeError("401 invalid_token")
        return 40
    async def deal_ids_after(self, *a): return []
    async def categories(self): return [("0", "Общая"), ("3", "Китай")]
    async def stages(self, cat): return [("NEW", "Новая"), ("NO_MESSENGER", "Не нашёл мессенджер")] if cat == "0" \
        else [("C3:NEW", "Новая"), ("C3:LOST", "Не дозвонились")]
    async def get_deal(self, i): raise RuntimeError("тестовый Битрикс")

class FakeOrbita:
    def __init__(self, url="", key="", channels=()): self.key = key
    async def account(self):
        if self.key == "bad": raise RuntimeError("HTTP 401 invalid_key")
        return {"name": "Тест", "subscription_active": True}
    async def channels(self): return [{"канал": "whatsapp", "подключён": True}, {"канал": "telegram", "подключён": False}]
    async def send_first(self, phone, text, key, name=None):
        return FirstSendResult("sent", "9", "whatsapp") if phone.endswith("1") else FirstSendResult("no_messenger")

ui_settings.Bitrix, ui_settings.OrbitaAPI = FakeBitrix, FakeOrbita
import first_contact.worker as worker_mod
worker_mod.Bitrix, worker_mod.OrbitaAPI = FakeBitrix, FakeOrbita

env = config.env(); db = DB(env.db_path); s = config.load(db)
w = Worker(env, s, db)
app = create_app(w)
H = {"Origin": "http://testserver"}
c = TestClient(app)
snap = lambda name, r: open(f"{tmp}/{name}.html", "w").write(r.text)

# --- первый запуск ---
r = c.get("/", follow_redirects=False)
check(r.status_code == 303 and r.headers["location"] == "/setup", "без администратора → /setup")
from first_contact.auth import Users
users = Users(db); code = users.setup_code()
snap("setup", c.get("/setup"))
r = c.post("/setup", data={"code": "нет", "email": "a@x.ru", "password": "12345678"}, headers=H, follow_redirects=False)
check("msg=%21" in r.headers["location"] and users.count() == 0, "неверный код — администратор не создан")
r = c.post("/setup", data={"code": code, "email": "a@x.ru", "password": "1234"}, headers=H, follow_redirects=False)
check(users.count() == 0, "короткий пароль не принят")
r = c.post("/setup", data={"code": code, "email": "A@x.ru", "password": "12345678", "name": "Илья"},
           follow_redirects=False)
check(users.count() == 0, "форма без Origin отвергнута")
r = c.post("/setup", data={"code": code, "email": "A@x.ru", "password": "12345678", "name": "Илья"}, headers=H,
           follow_redirects=False)
check(users.count() == 1 and r.headers["location"].startswith("/settings"), "администратор создан, переход в настройки")
check(users.setup_code() is None and c.get("/setup", follow_redirects=False).headers["location"] == "/login",
      "после создания /setup закрыт")

# --- настройки ---
r = c.get("/"); snap("index-onboarding", r)
check("Робот ещё не работает" in r.text and "Подключить Битрикс" in r.text, "главная: список шагов подключения")
snap("settings-empty", c.get("/settings"))
r = c.post("/settings/enable", headers=H, follow_redirects=False)
check(not s.enabled, "без подключений робот не включается")
r = c.post("/settings/bitrix", data={"webhook": "https://p.bitrix24.ru/rest/1/bad/"}, headers=H, follow_redirects=False)
check(not s.bitrix_webhook and "msg=%21" in r.headers["location"], "неверный вебхук Битрикса не сохранён")
r = c.post("/settings/bitrix", data={"webhook": "https://p.bitrix24.ru/rest/1/good"}, headers=H, follow_redirects=False)
check(s.bitrix_webhook == "https://p.bitrix24.ru/rest/1/good/" and not s.no_messenger_stage, "вебхук сохранён, этап ещё не выбран")
r = c.get("/settings")
check("Не нашёл мессенджер" in r.text and "rest/1/good" not in r.text, "этапы из Битрикса в списке, вебхук целиком не показан")
c.post("/settings/bitrix", data={"category": "0", "stage": "NO_MESSENGER"}, headers=H)
check(s.no_messenger_stage == "NO_MESSENGER" and s.deal_category_id == "0", "этап выбран")
c.post("/settings/bitrix", data={"category": "3", "stage": "NO_MESSENGER"}, headers=H)
check(s.deal_category_id == "3" and s.no_messenger_stage == "", "смена воронки сбрасывает этап")
c.post("/settings/bitrix", data={"category": "3", "stage": "C3:LOST"}, headers=H)
check(s.no_messenger_stage == "C3:LOST", "этап новой воронки выбран")
r = c.post("/settings/orbita", data={"api_key": "bad", "place_whatsapp": "1"}, headers=H, follow_redirects=False)
check(not s.orbita_api_key, "ключ, который Орбита не приняла, не сохранён")
c.post("/settings/orbita", data={"api_key": "orb_pk_secret123456", "place_whatsapp": "1", "place_telegram": "2",
                                 "webhook_secret": "whsec_1"}, headers=H)
check(s.orbita_api_key == "orb_pk_secret123456" and s.orbita_channels == ["whatsapp", "telegram"]
      and s.orbita_webhook_secret == "whsec_1", "Орбита: ключ, каналы, секрет вебхука")
r = c.get("/settings")
check("orb_pk_secret123456" not in r.text and "orb_pk_sec…" in r.text, "ключ Орбиты показан только началом")
c.post("/settings/orbita", data={"place_telegram": "1", "place_max": "2", "place_whatsapp": "3"}, headers=H)
check(s.orbita_channels == ["telegram", "max", "whatsapp"], "порядок мессенджеров задаётся местами")
r = c.get("/settings")
check("Сейчас: Telegram → Max → WhatsApp" in r.text, "текущий порядок показан в панели")
c.post("/settings/orbita", data={"place_telegram": "1", "place_max": "1"}, headers=H)
check(s.orbita_channels == ["telegram", "max", "whatsapp"], "одинаковые места не сохраняются")
c.post("/settings/orbita", data={}, headers=H)
check(s.orbita_channels == ["telegram", "max", "whatsapp"], "все «не искать» не сохраняются")
c.post("/settings/orbita", data={"place_whatsapp": "1", "webhook_secret_clear": "1"}, headers=H)
check(s.orbita_channels == ["whatsapp"], "«не искать» убирает мессенджер")
check(s.orbita_api_key == "orb_pk_secret123456" and s.orbita_webhook_secret == "", "пустое поле не стирает ключ, «стереть» стирает секрет")
check(config.load(db).orbita_api_key == "orb_pk_secret123456", "настройки сохранены в базе")

# сообщения
form = {"tpl_first": "{name}, привет {oops}", "tpl_followup": "x", "tpl_reply": "y", "followup_hours": "24",
        "reply_delay_minutes": "2"}
r = c.post("/settings/messages", data=form, headers=H, follow_redirects=False)
check("msg=%21" in r.headers["location"] and "{oops}" not in s.tpl_first, "шаблон с неизвестной подстановкой не сохранён")
form.update(tpl_first="{name}, здравствуйте!", tpl_reply="Спасибо, {name}!", reply_on="1")
c.post("/settings/messages", data=form, headers=H)
check(s.tpl_first == "{name}, здравствуйте!" and s.reply_on and not s.followup_on, "шаблоны и переключатели сохранены")
r = c.get("/settings")
check("Анна, здравствуйте!" in r.text and "Спасибо, Анна!" in r.text, "предпросмотр с подстановкой имени")
r = c.post("/settings/schedule", data={"start": "22:00", "end": "08:00", "tz": "Europe/Moscow"}, headers=H, follow_redirects=False)
check("msg=%21" in r.headers["location"], "окно наоборот не принято")
c.post("/settings/schedule", data={"start": "10:00", "end": "20:00", "tz": "Asia/Novosibirsk"}, headers=H)
check(s.window_start == "10:00" and s.tz_name == "Asia/Novosibirsk", "расписание сохранено")

# тест и включение
r = c.post("/settings/test", data={"phone": "8 999 000-00-01"}, headers=H, follow_redirects=False)
check("отправлено" in __import__("urllib.parse").parse.unquote(r.headers["location"]) and db.get("test_sent_at"),
      "тестовое сообщение отправлено")
r = c.post("/settings/test", data={"phone": "+79990000002"}, headers=H, follow_redirects=False)
check("msg=%21" in r.headers["location"], "тест: номера нет в мессенджерах — понятная ошибка")
c.post("/settings/enable", headers=H)
check(s.enabled and db.get("last_deal_id") == "40", "робот включён, сделки до 40 не трогает")
r = c.get("/"); snap("index", r)
check("робот включён" in r.text and "Робот ещё не работает" not in r.text, "главная без списка шагов")
snap("settings", c.get("/settings"))

# --- сделки ---
db.add_deal(41); db.update(41, status="needs_check", name="Сергей", phone="+7999", last_error="x")
db.add_deal(42); db.update(42, status="replied", name="Игорь", reply_due_at="2030-01-01T00:00:00+00:00")
for path in ["/?status=needs_check", "/?q=игорь", "/deal/41", "/deal/42", "/deal/999", "/events", "/status", "/account", "/team"]:
    r = c.get(path); assert r.status_code == 200, (path, r.status_code, r.text[:300]); snap(re.sub(r"\W", "_", path.strip("/")) or "x", r)
check(True, "все страницы открываются")
c.post("/deal/42/stop", headers=H)
check(db.deal(42)["status"] == "replied" and not db.deal(42)["reply_due_at"], "«остановить» отменяет ответ по шаблону")
c.post("/deal/41/replied", headers=H)
check(db.deal(41)["status"] == "replied", "«клиент ответил» вручную")

# --- команда ---
r = c.post("/team/invite", data={"role": "operator"}, headers=H)
link = re.search(r"/invite/([\w-]+)", r.text).group(1)
check(len(link) > 20, "ссылка-приглашение выдана")
check('id="team"' in r.text and "разделе «Команда»" in r.text, "приглашение показано в настройках, в разделе «Команда»")
check('href="/team"' not in c.get("/").text and 'href="#team"' in c.get("/settings").text,
      "«Команда» — раздел настроек, а не пункт верхнего меню")
r = c.get("/team", follow_redirects=False)
check(r.status_code == 303 and r.headers["location"] == "/settings#team", "старый адрес /team ведёт в настройки")
op = TestClient(app)
r = op.post(f"/invite/{link}", data={"email": "op@x.ru", "password": "operator1"}, headers=H, follow_redirects=False)
check(users.by_email("op@x.ru")["role"] == "operator", "оператор зарегистрирован по приглашению")
r = op.post(f"/invite/{link}", data={"email": "op2@x.ru", "password": "operator1"}, headers=H, follow_redirects=False)
check(users.by_email("op2@x.ru") is None, "приглашение одноразовое")
check(op.get("/").status_code == 200 and op.get("/settings").status_code == 403, "оператор видит сделки, но не настройки")
check(op.post("/settings/disable", headers=H).status_code == 403 and s.enabled, "оператор не может выключить робота")
check("Настройки" not in op.get("/").text, "у оператора нет пункта «Настройки»")
op.post("/logout", headers=H)
check(op.get("/", follow_redirects=False).status_code == 303, "после выхода — снова вход")

# --- вход ---
anon = TestClient(app)
for _ in range(10):
    anon.post("/login", data={"email": "a@x.ru", "password": "wrong-pass"}, headers=H)
r = anon.post("/login", data={"email": "a@x.ru", "password": "12345678"}, headers=H, follow_redirects=False)
check("%D0%A1%D0%BB%D0%B8%D1%88%D0%BA%D0%BE%D0%BC" in r.headers["location"], "после 10 ошибок вход заблокирован")
fresh = TestClient(create_app(w))
r = fresh.post("/login", data={"email": "a@x.ru", "password": "12345678"}, headers=H, follow_redirects=False)
check(r.headers["location"] == "/" and "fc_session" in r.cookies, "вход с верным паролем")
snap("login", anon.get("/login"))
check(c.post("/team/1/delete", headers=H, follow_redirects=False).status_code == 303 and users.count() == 2,
      "себя удалить нельзя")
# --- отключение ---
c.post("/settings/orbita/disconnect", headers=H)
check(not s.orbita_api_key and not s.enabled, "«Отключить Орбиту» стирает ключ и выключает робота")
c.post("/settings/bitrix/disconnect", headers=H)
check(not s.bitrix_webhook and not s.no_messenger_stage, "«Отключить Битрикс» стирает адрес и этап")
check("Подключить Битрикс" in c.get("/").text, "главная снова показывает шаги подключения")
print(f"\nВсе проверки пройдены: {ok}. Снимки страниц: {tmp}")
