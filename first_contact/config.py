"""Настройки.

Два источника:
- переменные окружения (файл .env) — только то, что нужно до запуска панели: где база,
  публичный адрес, порт. Меняются администратором сервера;
- база (таблица settings) — всё остальное: подключение к Битриксу и Орбите, шаблоны,
  расписание, включён ли робот. Меняются в панели, без перезапуска.

При самом первом запуске, пока в базе настроек нет, значения можно подложить из окружения
(BITRIX_WEBHOOK, ORBITA_API_KEY, …) — так переезжает установка, настроенная через .env.
"""

import json
import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ENV_FILES = [ROOT / ".env"]

CHANNELS = ["whatsapp", "max", "telegram"]   # что умеет «написать первым» в Орбите
CHANNEL_RU = {"whatsapp": "WhatsApp", "max": "Max", "telegram": "Telegram"}

DEFAULT_FIRST = (
    "{name}, здравствуйте! Вы оставили заявку на нашем сайте. Меня зовут Анна, я менеджер. "
    "Подскажите, удобно ли продолжить общение здесь?"
)
DEFAULT_FOLLOWUP = (
    "{name}, добрый день! Напоминаю о вашей заявке. Если вопрос ещё актуален — напишите, "
    "пожалуйста, пару слов, и я всё расскажу."
)
DEFAULT_REPLY = (
    "Спасибо, что ответили! Я уже передала вашу заявку менеджеру — он напишет вам в ближайшее время."
)


def _load_env_files() -> None:
    for path in ENV_FILES:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Env:
    """Из окружения. Не меняется без перезапуска."""
    db_path: Path
    public_url: str        # https://robot.company.ru — для адресов вебхуков в панели; пусто — берём из запроса
    port: int
    poll_seconds: int
    dry_run: bool          # ничего не отправлять и не менять в Битриксе (для отладки)


def env() -> Env:
    _load_env_files()
    e = os.environ.get
    public = e("PUBLIC_URL", "").rstrip("/")
    if not public and e("DOMAIN"):
        public = f"https://{e('DOMAIN')}"
    return Env(
        db_path=Path(e("DB_PATH", ROOT / "data" / "state.sqlite3")),
        public_url=public,
        port=int(e("PORT", "8080")),
        poll_seconds=int(e("POLL_SECONDS", "60")),
        dry_run=e("DRY_RUN", "0") == "1",
    )


@dataclass
class Settings:
    """Из базы. Меняются в панели («Настройки»), действуют сразу."""
    # Битрикс
    bitrix_webhook: str = ""          # входящий вебхук с правами crm: https://<портал>/rest/<user>/<код>/
    bitrix_app_token: str = ""        # application_token исходящего вебхука «Создание сделки»
    deal_category_id: str | None = None   # воронка; None — все
    no_messenger_stage: str = ""      # этап «Не нашёл мессенджер»
    # Орбита
    orbita_api_url: str = "https://crm.my-orbita.ru/api/public/v1"
    orbita_api_key: str = ""
    orbita_channels: list[str] = field(default_factory=lambda: list(CHANNELS))
    orbita_webhook_secret: str = ""
    # Сообщения
    tpl_first: str = DEFAULT_FIRST
    tpl_followup: str = DEFAULT_FOLLOWUP
    tpl_reply: str = DEFAULT_REPLY
    tpl_no_name: str = "Здравствуйте"
    followup_on: bool = True
    reply_on: bool = False
    followup_hours: float = 24
    reply_delay_minutes: float = 2
    # Расписание
    window_start: str = "09:00"
    window_end: str = "21:00"
    tz_name: str = "Europe/Moscow"
    # Робот
    enabled: bool = False

    @property
    def send_window(self) -> tuple[time, time]:
        return time.fromisoformat(self.window_start), time.fromisoformat(self.window_end)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    @property
    def bitrix_ok(self) -> bool:
        return bool(self.bitrix_webhook and self.no_messenger_stage)

    @property
    def orbita_ok(self) -> bool:
        return bool(self.orbita_api_key and self.orbita_channels)

    @property
    def configured(self) -> bool:
        return self.bitrix_ok and self.orbita_ok

    @property
    def portal(self) -> str:
        w = self.bitrix_webhook
        return w.split("/rest/")[0] if "/rest/" in w else ""


SECRET_FIELDS = {"bitrix_webhook", "bitrix_app_token", "orbita_api_key", "orbita_webhook_secret"}
_NAMES = {f.name for f in fields(Settings)}


def load(db) -> Settings:
    """Настройки из базы; при первом запуске — заготовка из окружения."""
    stored = db.settings()
    if not stored:
        stored = _seed_from_env()
        if stored:
            db.save_settings(stored)
    s = Settings()
    for k, v in stored.items():
        if k in _NAMES:
            setattr(s, k, v)
    return s


def save(db, s: Settings, **changes) -> None:
    for k, v in changes.items():
        if k not in _NAMES:
            raise KeyError(k)
        setattr(s, k, v)
    db.save_settings({k: getattr(s, k) for k in changes})


def as_dict(s: Settings) -> dict:
    return asdict(s)


def _seed_from_env() -> dict:
    """Установка, настроенная по-старому через .env (до версии с панелью настроек)."""
    e = os.environ.get
    seed: dict = {}
    pairs = {"BITRIX_WEBHOOK": "bitrix_webhook", "BITRIX_APP_TOKEN": "bitrix_app_token",
             "NO_MESSENGER_STAGE": "no_messenger_stage", "DEAL_CATEGORY_ID": "deal_category_id",
             "ORBITA_API_URL": "orbita_api_url", "ORBITA_API_KEY": "orbita_api_key",
             "ORBITA_WEBHOOK_SECRET": "orbita_webhook_secret", "TZ_NAME": "tz_name"}
    for var, key in pairs.items():
        if e(var):
            seed[key] = e(var)
    if seed.get("bitrix_webhook"):
        seed["bitrix_webhook"] = seed["bitrix_webhook"].rstrip("/") + "/"
        seed.setdefault("no_messenger_stage", "NO_MESSENGER")   # значение по умолчанию старых версий
    if e("ORBITA_CHANNELS"):
        seed["orbita_channels"] = [c.strip() for c in e("ORBITA_CHANNELS").split(",") if c.strip() in CHANNELS]
    if e("FOLLOWUP_HOURS"):
        seed["followup_hours"] = float(e("FOLLOWUP_HOURS"))
    if e("SEND_WINDOW"):
        start, end = e("SEND_WINDOW").split("-")
        seed["window_start"], seed["window_end"] = start.strip(), end.strip()
    old_templates = ROOT / "templates.toml"
    if old_templates.exists():
        t = tomllib.loads(old_templates.read_text(encoding="utf-8"))
        for old, new in (("first", "tpl_first"), ("followup", "tpl_followup"), ("no_name", "tpl_no_name")):
            if isinstance(t.get(old), str) and t[old].strip():
                seed[new] = t[old].strip()
    return seed


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
