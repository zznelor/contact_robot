"""Панель: настройки Битрикса, Орбиты, сообщений, расписания; тест, включение, отключение."""

import re

import pytest

from conftest import run
from first_contact import config, ui_settings
from first_contact import worker as worker_mod
from first_contact.bitrix import Bitrix

from test_bitrix import DOC_STAGES, method_of, ok, status

from test_ui_auth import is_error

GOOD_WEBHOOK = "https://p.bitrix24.ru/rest/1/good"
KEY = "orb_pk_secret123456"
MESSAGES = {"tpl_first": "{name}, здравствуйте!", "tpl_followup": "x", "tpl_reply": "Спасибо, {name}!",
            "followup_hours": "24", "reply_delay_minutes": "2", "reply_on": "1"}


def onboarding_shown(client) -> bool:
    """Список шагов подключения на главной — ссылки на разделы настроек."""
    return 'href="/settings#bitrix"' in client.get("/").text


# ---------- Битрикс ----------

def test_enable_without_connections_refused(panel, admin):
    r = admin.post("/settings/enable", follow_redirects=False)
    assert is_error(r) and not panel.s.enabled


def test_bad_bitrix_webhook_not_saved(panel, admin):
    r = admin.post("/settings/bitrix", data={"webhook": "https://p.bitrix24.ru/rest/1/bad/"}, follow_redirects=False)
    assert is_error(r) and not panel.s.bitrix_webhook


def test_bitrix_webhook_saved_stage_not_yet(panel, admin):
    admin.post("/settings/bitrix", data={"webhook": GOOD_WEBHOOK})
    assert panel.s.bitrix_webhook == GOOD_WEBHOOK + "/" and not panel.s.no_messenger_stage
    page = admin.get("/settings").text
    assert 'value="NO_MESSENGER"' in page, "этапы из Битрикса в списке"
    assert "rest/1/good" not in page, "вебхук целиком не показан"


def test_bitrix_webhook_saved_when_stages_unavailable(panel, admin):
    """Адрес рабочий, а этапы Битрикс не отдал — адрес сохраняется, этап выбирают позже."""
    r = admin.post("/settings/bitrix", data={"webhook": "https://p.bitrix24.ru/rest/1/nostages"},
                   follow_redirects=False)
    assert is_error(r)
    assert panel.s.bitrix_webhook == "https://p.bitrix24.ru/rest/1/nostages/" and not panel.s.no_messenger_stage


def test_bitrix_stage_and_funnel(panel, admin):
    admin.post("/settings/bitrix", data={"webhook": GOOD_WEBHOOK})
    admin.post("/settings/bitrix", data={"category": "0", "stage": "NO_MESSENGER"})
    assert (panel.s.deal_category_id, panel.s.no_messenger_stage) == ("0", "NO_MESSENGER")
    admin.post("/settings/bitrix", data={"category": "3", "stage": "NO_MESSENGER"})
    assert (panel.s.deal_category_id, panel.s.no_messenger_stage) == ("3", ""), "смена воронки сбрасывает этап"
    admin.post("/settings/bitrix", data={"category": "3", "stage": "C3:LOST"})
    assert panel.s.no_messenger_stage == "C3:LOST"


# ---------- Орбита ----------

def test_rejected_orbita_key_not_saved(panel, admin):
    admin.post("/settings/orbita", data={"api_key": "bad", "place_whatsapp": "1"})
    assert not panel.s.orbita_api_key


def test_orbita_key_channels_secret_saved(panel, admin):
    admin.post("/settings/orbita", data={"api_key": KEY, "place_whatsapp": "1", "place_telegram": "2",
                                         "webhook_secret": "whsec_1"})
    s = panel.s
    assert (s.orbita_api_key, s.orbita_channels, s.orbita_webhook_secret) == (KEY, ["whatsapp", "telegram"], "whsec_1")
    assert config.load(panel.db).orbita_api_key == KEY, "сохранено в базе"
    page = admin.get("/settings").text
    assert KEY not in page and KEY[:10] in page, "ключ показан только началом"


def places_shown(page: str) -> dict[str, str]:
    """Выбранное место каждого мессенджера в форме: {канал: "1" | "2" | "3" | "" (не искать)}."""
    return {ch: re.search(r'value="(\d*)" selected', body).group(1)
            for ch, body in re.findall(r'<select name="place_(\w+)"[^>]*>(.*?)</select>', page)}


def test_channel_order_by_places(panel, admin):
    admin.post("/settings/orbita", data={"api_key": KEY, "place_whatsapp": "1"})
    admin.post("/settings/orbita", data={"place_telegram": "1", "place_max": "2", "place_whatsapp": "3"})
    assert panel.s.orbita_channels == ["telegram", "max", "whatsapp"]
    assert places_shown(admin.get("/settings").text) == {"telegram": "1", "max": "2", "whatsapp": "3"}


@pytest.mark.parametrize("form", [
    {"place_telegram": "1", "place_max": "1"},   # одинаковые места
    {},                                           # всё «не искать»
])
def test_invalid_channel_places_not_saved(panel, admin, form):
    admin.post("/settings/orbita", data={"api_key": KEY, "place_telegram": "1", "place_max": "2",
                                         "place_whatsapp": "3"})
    r = admin.post("/settings/orbita", data=form, follow_redirects=False)
    assert is_error(r) and panel.s.orbita_channels == ["telegram", "max", "whatsapp"]


def test_skip_channel_empty_key_keeps_clear_erases_secret(panel, admin):
    admin.post("/settings/orbita", data={"api_key": KEY, "place_whatsapp": "1", "place_max": "2",
                                         "webhook_secret": "whsec_1"})
    admin.post("/settings/orbita", data={"place_whatsapp": "1", "webhook_secret_clear": "1"})
    s = panel.s
    assert s.orbita_channels == ["whatsapp"], "«не искать» убирает мессенджер"
    assert s.orbita_api_key == KEY, "пустое поле не стирает ключ"
    assert s.orbita_webhook_secret == ""


# ---------- сообщения и расписание ----------

def test_template_with_unknown_placeholder_not_saved(panel, admin):
    r = admin.post("/settings/messages", data={**MESSAGES, "tpl_first": "{name}, привет {oops}"},
                   follow_redirects=False)
    assert is_error(r) and "{oops}" not in panel.s.tpl_first


def test_templates_and_switches_saved_with_preview(panel, admin):
    admin.post("/settings/messages", data=MESSAGES)
    s = panel.s
    assert s.tpl_first == "{name}, здравствуйте!" and s.reply_on and not s.followup_on
    page = admin.get("/settings").text
    assert "Анна, здравствуйте!" in page and "Спасибо, Анна!" in page, "предпросмотр с подстановкой имени"


@pytest.mark.parametrize("hours, saved, error", [
    ("2,5", 2.5, False),     # запятая — тоже число
    ("abc", 24.0, True),
])
def test_followup_hours_parsing(panel, admin, hours, saved, error):
    r = admin.post("/settings/messages", data={**MESSAGES, "followup_hours": hours}, follow_redirects=False)
    assert is_error(r) is error and panel.s.followup_hours == saved


def test_incomplete_form_is_html_400(admin):
    r = admin.post("/settings/schedule", data={"start": "10:00"})
    assert r.status_code == 400 and "text/html" in r.headers["content-type"]


def test_schedule(panel, admin):
    r = admin.post("/settings/schedule", data={"start": "22:00", "end": "08:00", "tz": "Europe/Moscow"},
                   follow_redirects=False)
    assert is_error(r) and panel.s.window_start == "09:00", "окно наоборот не принято"
    admin.post("/settings/schedule", data={"start": "10:00", "end": "20:00", "tz": "Asia/Novosibirsk"})
    assert (panel.s.window_start, panel.s.window_end, panel.s.tz_name) == ("10:00", "20:00", "Asia/Novosibirsk")


# ---------- тест, включение, отключение ----------

def test_test_message_sent(configured, admin):
    r = admin.post("/settings/test", data={"phone": "8 999 000-00-01"}, follow_redirects=False)
    assert not is_error(r) and configured.db.get("test_sent_at")
    assert [p for p, _, _ in configured.worker.orbita.sent] == ["+79990000001"]


def test_test_message_number_nowhere(configured, admin):
    r = admin.post("/settings/test", data={"phone": "+79990000002"}, follow_redirects=False)
    assert is_error(r) and not configured.db.get("test_sent_at")


def test_enable_remembers_last_deal_and_hides_onboarding(configured, admin):
    assert onboarding_shown(admin)
    admin.post("/settings/enable")
    assert configured.s.enabled and configured.db.get("last_deal_id") == "40"
    assert not onboarding_shown(admin)


def test_tick_without_configuration_refused(configured, admin):
    run(configured.worker.enable())
    configured.s.no_messenger_stage = ""
    assert is_error(admin.post("/actions/tick", data={"step": "all"}, follow_redirects=False))


def test_disconnect_orbita_and_bitrix(configured, admin):
    run(configured.worker.enable())
    admin.post("/settings/orbita/disconnect")
    assert not configured.s.orbita_api_key and not configured.s.enabled
    admin.post("/settings/bitrix/disconnect")
    assert not configured.s.bitrix_webhook and not configured.s.no_messenger_stage
    assert onboarding_shown(admin)


def test_test_message_not_sent_in_dry_run(configured, admin, monkeypatch):
    """DRY_RUN — «ничего не отправлять»: тестовое сообщение тоже не уходит и шаг подключения не засчитан."""
    monkeypatch.setattr(configured.worker.env, "dry_run", True)
    r = admin.post("/settings/test", data={"phone": "8 999 000-00-01"}, follow_redirects=False)
    assert is_error(r)
    assert configured.worker.orbita.sent == [] and not configured.db.get("test_sent_at")


# ---------- этап «Не нашёл мессенджер» — только в работе (настоящий клиент Битрикса) ----------

NO_MESSENGER = status("NO_MESSENGER", "Не нашёл мессенджер", None, 45)


def bitrix_portal(req):
    """Портал по документации: воронка «Общая» (crm.category.list), её этапы — пример из статьи crm.status.list и
    свой этап в работе «Не нашёл мессенджер»."""
    m = method_of(req)
    if m == "crm.category.list":
        return ok({"categories": [{"id": 0, "name": "Общая", "sort": 100, "entityTypeId": 2, "isDefault": "Y"}]})
    if m == "crm.status.list":
        return ok(sorted([*DOC_STAGES, NO_MESSENGER], key=lambda r: int(r["SORT"])))
    if m == "crm.deal.list":
        return ok([{"ID": "40"}])
    return ok(True)


@pytest.fixture
def real_bitrix(panel, http, monkeypatch):
    monkeypatch.setattr(ui_settings, "Bitrix", Bitrix)
    monkeypatch.setattr(worker_mod, "Bitrix", Bitrix)
    http(bitrix_portal)
    panel.worker.save(bitrix_webhook="https://p.bitrix24.ru/rest/1/good/", orbita_api_key="orb_pk_secret123456",
                      orbita_channels=["whatsapp"])
    return panel


def test_closing_stage_not_offered(real_bitrix, admin):
    page = admin.get("/settings").text
    assert 'value="NO_MESSENGER"' in page and 'value="NEW"' in page
    assert not any(f'value="{s}"' in page for s in ("WON", "LOSE", "APOLOGY"))


@pytest.mark.parametrize("stage", ["LOSE", "WON", "APOLOGY"])
def test_closing_stage_rejected_from_forged_form(real_bitrix, admin, stage):
    r = admin.post("/settings/bitrix", data={"category": "0", "stage": stage}, follow_redirects=False)
    assert is_error(r) and real_bitrix.s.no_messenger_stage == ""
    admin.post("/settings/bitrix", data={"category": "0", "stage": "NO_MESSENGER"})
    assert real_bitrix.s.no_messenger_stage == "NO_MESSENGER"


def test_saved_closing_stage_is_setup_error(real_bitrix, admin):
    """Установка старой версии сохранила провальный этап: панель показывает ошибку настройки, робот не включается."""
    real_bitrix.worker.save(no_messenger_stage="LOSE")
    assert 'id="stage-problem"' in admin.get("/settings").text
    r = admin.post("/settings/enable", follow_redirects=False)
    assert is_error(r) and not real_bitrix.s.enabled
    real_bitrix.worker.save(no_messenger_stage="NO_MESSENGER")
    assert 'id="stage-problem"' not in admin.get("/settings").text
    admin.post("/settings/enable")
    assert real_bitrix.s.enabled
