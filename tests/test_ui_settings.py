"""Панель: настройки Битрикса, Орбиты, сообщений, расписания; тест, включение, отключение."""

import re
from html.parser import HTMLParser

import pytest

from conftest import run
from first_contact import config, ui_base, ui_settings
from first_contact import worker as worker_mod
from first_contact.bitrix import Bitrix

from test_bitrix import DOC_STAGES, method_of, ok, status

from test_ui_auth import is_error

GOOD_WEBHOOK = "https://p.bitrix24.ru/rest/1/good"
KEY = "orb_pk_secret123456"
MESSAGES = {"tpl_first": "{name}, здравствуйте!", "tpl_followup": "x", "tpl_reply": "Спасибо, {name}!",
            "followup_hours": "24", "reply_delay_minutes": "2", "reply_on": "1"}


class _Forms(HTMLParser):
    """Поля форм страницы так, как их отправит браузер: {action: (значения, имена всех полей)}.
    Непомеченный флажок не отправляется; у списка без selected уходит первый вариант."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: dict[str, tuple[dict, set]] = {}
        self._form = self._select = self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._form = self.forms.setdefault(a.get("action", ""), ({}, set()))
        elif self._form is None or "name" not in a and tag != "option":
            return
        elif tag == "input":
            values, names = self._form
            names.add(a["name"])
            if a.get("type") != "checkbox" or "checked" in a:
                values[a["name"]] = a.get("value", "")
        elif tag == "textarea":
            self._textarea = a["name"]
            self._form[1].add(a["name"])
            self._form[0][a["name"]] = ""
        elif tag == "select":
            self._select = [a["name"], None, None]       # имя, первый вариант, выбранный
            self._form[1].add(a["name"])
        elif tag == "option" and self._select is not None:
            value = a.get("value", "")
            if self._select[1] is None:
                self._select[1] = value
            if "selected" in a:
                self._select[2] = value

    def handle_data(self, data):
        if self._textarea is not None:
            self._form[0][self._textarea] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "textarea":
            self._textarea = None
        elif tag == "select" and self._select is not None:
            name, first, chosen = self._select
            if chosen is not None or first is not None:
                self._form[0][name] = chosen if chosen is not None else first
            self._select = None


def submit(client, action: str, **changes):
    """Открыть настройки и отправить форму action, как браузер: поля как на странице, плюс changes (None — снять
    флажок). Менять можно только поля, которые на странице есть."""
    parser = _Forms()
    parser.feed(client.get("/settings").text)
    values, names = parser.forms[action]
    assert set(changes) <= names, f"в форме {action} нет полей {set(changes) - names}"
    for k, v in changes.items():
        if v is None:
            values.pop(k, None)
        else:
            values[k] = v
    return client.post(action, data=values, follow_redirects=False)


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
    assert 'id="stage-problem"' not in page, "этап ещё не выбран — это не ошибка настройки"


def test_settings_open_when_bitrix_gives_no_stages(panel, admin):
    """Битрикс не отдал этапы — страница настроек открывается, а сохранённый этап ошибкой не объявляется:
    проверить его нечем."""
    panel.worker.save(bitrix_webhook="https://p.bitrix24.ru/rest/1/nostages/", no_messenger_stage="NO_MESSENGER")
    r = admin.get("/settings")
    assert r.status_code == 200 and 'id="stage-problem"' not in r.text


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
    assert "whsec_1" not in page, "секрет вебхуков целиком не показан"


@pytest.mark.parametrize("secret", ["Zq9x", "Zq9xW7kP3m", "Zq9xW7kP3mT5vR", "Zq9xW7kP3mT5vRa"])   # 4, 10, 14, 15
def test_saved_secret_not_shown_whole(panel, admin, secret):
    """Секреты после сохранения не показываются целиком (ui_settings.py) — при любой длине."""
    panel.worker.save(bitrix_webhook=f"https://p.bitrix24.ru/rest/1/{secret}/", bitrix_app_token=secret,
                      orbita_api_key=secret, orbita_webhook_secret=secret)
    assert secret not in admin.get("/settings").text


@pytest.mark.parametrize("secret", ["k", "k7", "k7Q"])
def test_short_secret_not_shown_whole(secret):
    """Секрет в интерфейсе — только начало (ui_base.mask): и у ключа короче 4 символов, где «начало» — весь ключ."""
    assert secret not in ui_base.mask(secret)


@pytest.mark.parametrize("headers, base", [
    ({}, "http://testserver"),
    ({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "robot.company.ru"}, "https://robot.company.ru"),
])
def test_webhook_addresses_shown_for_this_panel(panel, admin, headers, base):
    """docs/НАСТРОЙКА.md: адреса исходящего вебхука Битрикса и вебхука Орбиты берут из настроек панели."""
    page = admin.get("/settings", headers=headers).text
    assert f"{base}/bitrix/deal-add" in page and f"{base}/orbita/webhook" in page


def test_message_shown_escaped(admin):
    """Сообщение после формы приходит в адресе — его может подставить и чужая ссылка: выводится как текст."""
    page = admin.get("/settings", params={"msg": '!<img src=x onerror="alert(1)">'}).text
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in page and "<img src=x" not in page


# ---------- формы настроек: что на странице, то и сохраняется ----------

VARIANTS = {
    # Значения нарочно не первые и не последние в своих списках формы (кроме воронки — её два варианта ниже): форма,
    # которая потеряла отметку selected или отметила всё, отправит другое значение
    "a": dict(deal_category_id="3", no_messenger_stage="C3:NEW", orbita_channels=["max"], followup_on=True,
              reply_on=True, tz_name="Europe/Moscow"),
    # Часовой пояс не из списка панели (задан до панели, переменной TZ_NAME), флажки сняты
    "b": dict(deal_category_id="0", no_messenger_stage="NEW", orbita_channels=["telegram", "whatsapp"],
              followup_on=False, reply_on=False, tz_name="Etc/GMT-3"),
}


def fill(panel, variant: str = "a"):
    """Все разделы настроек заполнены непохожими на умолчания значениями; включены не все мессенджеры."""
    panel.worker.save(
        bitrix_app_token="app-token-1", orbita_webhook_secret="whsec_saved",
        tpl_first="{name}, добрый день!\nЭто первое.", tpl_followup="{name}, напоминаем о {deal_title}",
        tpl_reply="Спасибо, {name}!", tpl_no_name="Добрый день", followup_hours=2.5, reply_delay_minutes=0.5,
        window_start="07:30", window_end="19:45", **VARIANTS[variant])
    return panel


@pytest.fixture
def filled(configured, admin):
    return fill(configured)


SAVED = ("deal_category_id", "no_messenger_stage", "bitrix_webhook", "bitrix_app_token", "orbita_api_key",
         "orbita_channels", "orbita_webhook_secret", "tpl_first", "tpl_followup", "tpl_reply", "tpl_no_name",
         "followup_on", "reply_on", "followup_hours", "reply_delay_minutes", "window_start", "window_end", "tz_name")


def saved(panel) -> dict:
    s = config.load(panel.db)
    return {k: getattr(s, k) for k in SAVED}


@pytest.mark.parametrize("variant", sorted(VARIANTS))
@pytest.mark.parametrize("action", ["/settings/bitrix", "/settings/orbita", "/settings/messages", "/settings/schedule"])
def test_form_saved_without_changes_keeps_settings(configured, admin, action, variant):
    """«Сохранить» без правок ничего не меняет: форма показывает сохранённые значения и отправляет их же."""
    before = saved(fill(configured, variant))
    r = submit(admin, action)
    assert r.status_code == 303 and not is_error(r)
    assert saved(configured) == before


@pytest.mark.parametrize("action, changes, expected", [
    ("/settings/bitrix", {"stage": "C3:LOST"}, {"no_messenger_stage": "C3:LOST"}),
    ("/settings/bitrix", {"webhook": "https://p.bitrix24.ru/rest/1/other"},
     {"bitrix_webhook": "https://p.bitrix24.ru/rest/1/other/"}),
    ("/settings/bitrix", {"app_token": "app-token-2"}, {"bitrix_app_token": "app-token-2"}),
    ("/settings/bitrix", {"app_token_clear": "1"}, {"bitrix_app_token": ""}),
    ("/settings/orbita", {"place_telegram": "2"}, {"orbita_channels": ["max", "telegram"]}),
    ("/settings/orbita", {"api_key": "orb_pk_other123456"}, {"orbita_api_key": "orb_pk_other123456"}),
    ("/settings/orbita", {"webhook_secret": "whsec_new"}, {"orbita_webhook_secret": "whsec_new"}),
    ("/settings/orbita", {"webhook_secret_clear": "1"}, {"orbita_webhook_secret": ""}),
    ("/settings/messages", {"followup_on": None, "tpl_reply": "Ответим, {name}"},
     {"followup_on": False, "tpl_reply": "Ответим, {name}"}),
])
def test_form_field_reaches_saved_setting(filled, admin, action, changes, expected):
    """Поле формы на странице меняет ту настройку, которую называет."""
    before = saved(filled)
    r = submit(admin, action, **changes)
    assert r.status_code == 303 and not is_error(r)
    assert saved(filled) == {**before, **expected}


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
    assert not is_error(submit(admin, "/settings/bitrix", stage="NO_MESSENGER")), "исправляется в той же форме"
    assert real_bitrix.s.no_messenger_stage == "NO_MESSENGER"
    assert 'id="stage-problem"' not in admin.get("/settings").text
    admin.post("/settings/enable")
    assert real_bitrix.s.enabled
