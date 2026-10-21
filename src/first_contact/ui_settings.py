"""Панель: настройки (робот, Битрикс, Орбита, сообщения, расписание) и команда. Только для администратора.

Всё, что меняется здесь, хранится в базе и действует сразу, без перезапуска.
Секреты после сохранения не показываются целиком; пустое поле — «не менять».
"""

import json
import time
from zoneinfo import ZoneInfo, available_timezones

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from .auth import ROLES
from .bitrix import Bitrix, BitrixError, normalize_phone
from .config import CHANNEL_RU, CHANNELS
from .orbita_api import OrbitaAPI
from .ui_base import Ctx, e, go, mask
from .worker import render

TIMEZONES = ["Europe/Kaliningrad", "Europe/Moscow", "Europe/Samara", "Asia/Yekaterinburg", "Asia/Omsk",
             "Asia/Novosibirsk", "Asia/Krasnoyarsk", "Asia/Irkutsk", "Asia/Yakutsk", "Asia/Vladivostok",
             "Asia/Magadan", "Asia/Kamchatka", "Europe/Minsk", "Asia/Almaty", "Asia/Tashkent", "Europe/Istanbul",
             "Asia/Dubai", "Asia/Shanghai"]
SAMPLE_NAME, SAMPLE_TITLE = "Анна", "Заявка с сайта"


def _secret_input(name: str, current: str, placeholder: str, clearable: bool = False) -> str:
    hint = (f'<span class="hint">Сохранён: <code>{e(mask(current))}</code> — оставьте поле пустым, чтобы не менять.</span>'
            if current else "")
    clear = (f'<label class="check"><input type="checkbox" name="{name}_clear" value="1"> стереть</label>'
             if clearable and current else "")
    # Обычное текстовое поле: и type="password", и точки через -webkit-text-security Safari принимает
    # за пароль и на каждое «Сохранить» спрашивает «Сохранить пароль?». После сохранения поле пустое.
    return (f'<input type="text" name="{name}" placeholder="{e(placeholder)}" autocomplete="off" '
            f'autocapitalize="off" spellcheck="false" data-1p-ignore data-lpignore="true" data-bwignore>'
            f'{hint}{clear}')


def _disconnect_btn(action: str, what: str) -> str:
    return (f'<button class="danger" formaction="{action}" formnovalidate '
            f'onclick="return confirm(\'Отключить {what}? Робот будет выключен, сохранённые ключи сотрутся.\')">'
            f'Отключить</button>')


def build_settings_router(ctx: Ctx) -> APIRouter:
    worker, users = ctx.worker, ctx.users
    db = worker.db
    router = APIRouter()
    admin_dep = ctx.admin_dep()

    # ---------- страница ----------

    async def bitrix_live():
        """Воронки и этапы в работе выбранной воронки из Битрикса — (воронки, этапы); не ответил — исключение."""
        try:
            return await worker.bitrix.categories(), await worker.bitrix.stages(worker.s.category)
        except Exception as ex:  # noqa: BLE001
            return ex

    def stage_problem(live) -> str:
        """Сохранённый этап «Не нашёл мессенджер» не среди этапов в работе: закрывающий (сохранён версией без
        проверки) или удалён в Битриксе. Пусто — этап в порядке или проверить не удалось."""
        s = worker.s
        if not s.no_messenger_stage or not isinstance(live, tuple) or s.no_messenger_stage in dict(live[1]):
            return ""
        return (f"Этап «Не нашёл мессенджер» ({s.no_messenger_stage}) — не этап в работе этой воронки: он закрывает "
                "сделку (успех или провал) или удалён. Перенесённую на закрывающий этап сделку робот не вернёт и "
                "после «Повторить». Выберите этап в работе и сохраните.")

    async def robot_section(problem: str = "") -> str:
        s = worker.s
        steps = [(s.bitrix_ok and not problem, "Битрикс подключён, этап «Не нашёл мессенджер» выбран", "#bitrix"),
                 (s.orbita_ok, "Орбита подключена", "#orbita"),
                 (bool(db.get("test_sent_at")), "Тестовое сообщение отправлено", "#test")]
        lis = "".join(f'<li><span class="m {"ok" if done else "muted"}">{"✓" if done else "○"}</span>'
                      f'<a href="{href}">{e(t)}</a></li>' for done, t, href in steps)
        if s.enabled:
            state = ('<p><span class="badge b-ok">включён</span> Робот пишет клиентам по новым сделкам.</p>'
                     '<form method="post" action="/settings/disable"><button class="danger">Выключить робота</button>'
                     ' <span class="hint">Новые сделки перестанут обрабатываться. Уже начатые диалоги тоже замрут.</span></form>')
        elif s.configured:
            state = ('<p><span class="badge b-off">выключен</span></p>'
                     '<form method="post" action="/settings/enable"><button class="primary">Включить робота</button>'
                     ' <span class="hint">Робот начнёт писать по сделкам, созданным после включения. '
                     'Старые сделки не тронет.</span></form>')
        else:
            state = '<p><span class="badge b-off">выключен</span> Сначала подключите Битрикс и Орбиту.</p>'
        if problem:
            state += ('<p class="bad" role="alert">Ошибка настройки: этап «Не нашёл мессенджер» не в работе — '
                      '<a href="#bitrix">исправьте</a>. '
                      + ("Робот работает, но переносит сделки на этот этап." if s.enabled else "Робот не включится.")
                      + '</p>')
        last_test = db.get("test_result") or ""
        test_disabled = "" if s.orbita_ok else "disabled"
        return f"""<div class="card" id="robot"><h2 style="margin-top:0">Робот</h2>{state}
<ol class="steps">{lis}</ol></div>
<div class="card" id="test"><h2 style="margin-top:0">Тестовое сообщение</h2>
<p class="muted">Отправит первое сообщение по текущему шаблону на указанный номер — как будто пришла заявка.
Сделка в Битриксе не создаётся. Берите свой номер или номер коллеги.</p>
<form method="post" action="/settings/test" class="bar">
<label><span class="visually-hidden">Телефон</span><input type="tel" name="phone" placeholder="+7 900 000-00-00" required {test_disabled}></label>
<label><span class="visually-hidden">Имя</span><input type="text" name="name" placeholder="Имя (по желанию)" {test_disabled}></label>
<button class="primary" {test_disabled}>Отправить</button></form>
{f'<p class="hint">Последний тест: {e(last_test)}</p>' if last_test else ''}</div>"""

    async def bitrix_section(base: str, fetched, problem: str) -> str:
        s = worker.s
        live = ""
        if worker.bitrix:
            try:
                if isinstance(fetched, Exception):
                    raise fetched
                cats, stages = fetched
                cat = s.category
                cat_opts = "".join(f'<option value="{e(i)}" {"selected" if i == cat else ""}>{e(n)}</option>'
                                   for i, n in cats)
                st_opts = '<option value="">— выберите —</option>' + "".join(
                    f'<option value="{e(i)}" {"selected" if i == s.no_messenger_stage else ""}>{e(n)}</option>'
                    for i, n in stages)
                live = f"""<label class="field"><span>Воронка</span><select name="category">{cat_opts}</select>
<span class="hint">Робот пишет только по сделкам этой воронки.</span></label>
<label class="field"><span>Этап «Не нашёл мессенджер»</span><select name="stage">{st_opts}</select>
<span class="hint">Куда переносить сделку, если номера клиента нет ни в одном мессенджере. В списке — только этапы
в работе: закрывающий этап закрыл бы сделку, и «Повторить» её бы не вернул. Нет такого этапа —
создайте его в Битриксе (CRM → Сделки → Настроить воронку) и обновите страницу.
После смены воронки сохраните и выберите этап заново.</span></label>"""
                if problem:
                    live += f'<p class="bad" role="alert" id="stage-problem">{e(problem)}</p>'
            except Exception as ex:  # noqa: BLE001
                live = f'<p class="bad">Битрикс не отвечает: {e(type(ex).__name__)}: {e(ex)}</p>'
        status = (f'<span class="badge b-ok">подключён</span> <span class="muted">{e(s.portal)}</span>'
                  if s.bitrix_webhook else '<span class="badge b-warn">не подключён</span>')
        disconnect = _disconnect_btn("/settings/bitrix/disconnect", "Битрикс") if s.bitrix_webhook else ""
        return f"""<form class="card" id="bitrix" method="post" action="/settings/bitrix">
<h2 style="margin-top:0">Битрикс</h2><p>{status}</p>
<label class="field"><span>Адрес входящего вебхука</span>
{_secret_input("webhook", s.bitrix_webhook, "https://ваш-портал.bitrix24.ru/rest/1/abc123…/")}
<span class="hint">Битрикс → Разработчикам → Другое → Входящий вебхук, права «CRM (crm)». Подробно — docs/НАСТРОЙКА.md.</span></label>
{live}
<h3>События о новых сделках (по желанию)</h3>
<p class="hint">Без них робот проверяет Битрикс раз в минуту. С ними — пишет клиенту в ту же секунду.
Битрикс → Разработчикам → Другое → Исходящий вебхук, событие «Создание сделки» (ONCRMDEALADD), адрес:</p>
<p><code>{e(base)}/bitrix/deal-add</code></p>
<label class="field"><span>Токен приложения (application_token) исходящего вебхука</span>
{_secret_input("app_token", s.bitrix_app_token, "токен из настроек исходящего вебхука", clearable=True)}</label>
<div class="bar"><button class="primary">Сохранить</button>{disconnect}</div></form>"""

    async def orbita_section(base: str) -> str:
        s = worker.s
        live = ""
        if worker.orbita:
            try:
                acc = await worker.orbita.account()
                chs = await worker.orbita.channel_status()
                live = (f'<p>Аккаунт «{e(acc.get("name"))}», подписка '
                        f'{"<span class=ok>активна</span>" if acc.get("subscription_active") else "<span class=bad>неактивна</span>"}. '
                        "Каналы: " + ", ".join(
                            f'{e(CHANNEL_RU.get(c, c))} — {"<span class=ok>подключён</span>" if ok else "<span class=muted>не подключён</span>" if ok is None else "<span class=bad>отключился</span>"}'
                            for c, ok in chs.items()) + "</p>")
            except Exception as ex:  # noqa: BLE001
                live = f'<p class="bad">Орбита не отвечает: {e(type(ex).__name__)}: {e(ex)}</p>'
        status = ('<span class="badge b-ok">подключена</span>' if s.orbita_api_key
                  else '<span class="badge b-warn">не подключена</span>')
        disconnect = _disconnect_btn("/settings/orbita/disconnect", "Орбиту") if s.orbita_api_key else ""
        # Строки в текущем порядке поиска, выключенные — в конце
        rows = [*s.orbita_channels, *(c for c in CHANNELS if c not in s.orbita_channels)]

        def place_select(c: str) -> str:
            cur = s.orbita_channels.index(c) + 1 if c in s.orbita_channels else 0
            opts = "".join(f'<option value="{n}" {"selected" if n == cur else ""}>{n}-й</option>'
                           for n in range(1, len(CHANNELS) + 1))
            off = f'<option value="" {"selected" if not cur else ""}>не искать</option>'
            return (f'<label class="check" style="align-items:center"><select name="place_{c}" style="width:auto">'
                    f'{opts}{off}</select> {CHANNEL_RU[c]}</label>')
        checks = "".join(place_select(c) for c in rows)
        order_now = " → ".join(CHANNEL_RU[c] for c in s.orbita_channels) or "не выбрано"
        return f"""<form class="card" id="orbita" method="post" action="/settings/orbita">
<h2 style="margin-top:0">Орбита</h2><p>{status}</p>{live}
<label class="field"><span>Ключ API</span>
{_secret_input("api_key", s.orbita_api_key, "orb_pk_…")}
<span class="hint">Кабинет Орбиты → Разработчикам → Ключи API. Права: «Диалоги: чтение» (dialogs:read),
«Ответ в существующий диалог» (messages:send), «Первое сообщение новому человеку» (dialogs:start).</span></label>
<div class="field"><span>Где искать клиента</span>{checks}
<span class="hint">Сейчас: {e(order_now)}. Робот ищет номер в мессенджерах по этому порядку и пишет в первый,
где нашёл. «Не искать» — мессенджер пропускается. Telegram лучше ставить последним: Орбита разрешает искать
в нём не больше 30 номеров в сутки.</span></div>
<h3>Вебхуки Орбиты (рекомендуется)</h3>
<p class="hint">Без них робот узнаёт об ответе клиента только при проверке истории (раз в 5 минут, если включён
ответ по шаблону, иначе — перед напоминанием). Кабинет Орбиты → Разработчикам → Вебхуки → Добавить,
события message.received, message.sent, message.failed, адрес:</p>
<p><code>{e(base)}/orbita/webhook</code></p>
<label class="field"><span>Секрет подписки</span>
{_secret_input("webhook_secret", s.orbita_webhook_secret, "whsec_…", clearable=True)}</label>
<div class="bar"><button class="primary">Сохранить</button>{disconnect}</div></form>"""

    def messages_section() -> str:
        s = worker.s

        def preview(tpl: str) -> str:
            try:
                return e(render(tpl, SAMPLE_NAME, SAMPLE_TITLE))
            except (KeyError, IndexError, ValueError) as ex:
                return f'<span class="bad">Ошибка в шаблоне: {e(ex)}</span>'

        def block(key: str, title: str, text: str, toggle: str | None, on: bool, help_: str) -> str:
            sw = (f'<label class="check"><input type="checkbox" name="{toggle}" value="1" {"checked" if on else ""}>'
                  f' отправлять</label>') if toggle else '<p class="hint">Отправляется всегда, пока робот включён.</p>'
            return f"""<div class="grid2" style="margin-bottom:16px"><div><h3>{title}</h3>{sw}
<textarea name="{key}">{e(text)}</textarea><p class="hint">{help_}</p></div>
<div><h3 class="muted">Как увидит клиент</h3><div class="preview">{preview(text)}</div></div></div>"""

        return f"""<form class="card" id="messages" method="post" action="/settings/messages">
<h2 style="margin-top:0">Сообщения</h2>
<p class="hint">Подстановки: <code>{{name}}</code> — имя клиента из контакта Битрикса, <code>{{deal_title}}</code> —
название сделки. Фигурные скобки для других целей пишите двойными: <code>{{{{</code> и <code>}}}}</code>.</p>
{block("tpl_first", "1. Первое сообщение", s.tpl_first, None, True,
       "Уходит клиенту, как только в Битриксе появилась сделка (в пределах окна отправки).")}
{block("tpl_followup", "2. Напоминание, если клиент молчит", s.tpl_followup, "followup_on", s.followup_on,
       "Уходит один раз, если клиент не ответил на первое сообщение. Не уходит, если его срок прошёл больше "
       "48 часов назад (робот был выключен).")}
<label class="field" style="max-width:280px"><span>Через сколько часов напоминать</span>
<input type="number" name="followup_hours" min="0.05" max="720" step="0.05" value="{s.followup_hours:g}"></label>
{block("tpl_reply", "3. Ответ на первое сообщение клиента", s.tpl_reply, "reply_on", s.reply_on,
       "Уходит один раз, когда клиент впервые написал в ответ. Не уходит, если менеджер успел ответить сам "
       "за время паузы, и если робот опоздал с ответом больше чем на 30 минут после паузы (например, был выключен). "
       "{deal_title} здесь пустой.")}
<label class="field" style="max-width:280px"><span>Пауза перед ответом, минут</span>
<input type="number" name="reply_delay_minutes" min="0" max="60" step="0.5" value="{s.reply_delay_minutes:g}"></label>
<label class="field" style="max-width:420px"><span>Обращение, если в контакте нет имени</span>
<input type="text" name="tpl_no_name" value="{e(s.tpl_no_name)}">
<span class="hint">Подставляется вместо {{name}}. Например: «Здравствуйте».</span></label>
<button class="primary">Сохранить</button></form>"""

    def schedule_section() -> str:
        s = worker.s
        zones = TIMEZONES if s.tz_name in TIMEZONES else [s.tz_name, *TIMEZONES]
        opts = "".join(f'<option value="{z}" {"selected" if z == s.tz_name else ""}>{z}</option>' for z in zones)
        return f"""<form class="card" id="schedule" method="post" action="/settings/schedule">
<h2 style="margin-top:0">Расписание</h2>
<p class="hint">Первое сообщение и напоминание уходят только в это время. Заявка ночью получит сообщение утром
(если ей ещё нет 48 часов). Ответ по шаблону уходит в любое время — клиент только что написал сам.</p>
<div class="bar"><label class="field"><span>С</span><input type="time" name="start" value="{e(s.window_start)}"></label>
<label class="field"><span>До</span><input type="time" name="end" value="{e(s.window_end)}"></label>
<label class="field"><span>Часовой пояс</span><select name="tz">{opts}</select></label></div>
<button class="primary">Сохранить</button></form>"""

    async def settings_page(request: Request, user, msg: str = "", new_link: str = ""):
        base = ctx.public_url(request)
        toc = "".join(f'<a href="#{a}">{t}</a>' for a, t in (
            ("robot", "Робот"), ("bitrix", "Битрикс"), ("orbita", "Орбита"), ("messages", "Сообщения"),
            ("schedule", "Расписание"), ("team", "Команда")))
        fetched = await bitrix_live() if worker.bitrix else None
        problem = stage_problem(fetched)
        body = (f'<h1>Настройки</h1><p class="muted">Изменения действуют сразу, перезапуск не нужен.</p>'
                f'<div class="toc">{toc}</div>{await robot_section(problem)}{await bitrix_section(base, fetched, problem)}'
                f'{await orbita_section(base)}{messages_section()}{schedule_section()}'
                f'{team_section(user, new_link)}')
        return ctx.page(user, "Настройки", "settings", body, msg)

    @router.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request, msg: str = "", user=Depends(admin_dep)):
        return await settings_page(request, user, msg)

    # ---------- сохранение ----------

    @router.post("/settings/bitrix")
    async def save_bitrix(webhook: str = Form(""), app_token: str = Form(""), app_token_clear: str = Form(""),
                          category: str = Form(None), stage: str = Form(None), user=Depends(admin_dep)):
        s = worker.s
        changes: dict = {}
        webhook = webhook.strip()
        client = worker.bitrix
        if webhook:
            if "/rest/" not in webhook or not webhook.startswith("https://"):
                return go("/settings#bitrix", "!Адрес вебхука должен выглядеть как https://портал.bitrix24.ru/rest/1/код/")
            client = Bitrix(webhook)
            try:
                await client.max_deal_id()
            except Exception as ex:  # noqa: BLE001
                return go("/settings#bitrix", f"!Битрикс не принял адрес: {ex}. Проверьте, что у вебхука есть права crm.")
            changes["bitrix_webhook"] = webhook.rstrip("/") + "/"
        if app_token.strip():
            changes["bitrix_app_token"] = app_token.strip()
        elif app_token_clear:
            changes["bitrix_app_token"] = ""
        msg = "Сохранено"
        current_cat = s.category
        if webhook:
            # Новый адрес — списки воронок и этапов на странице были от старого; оставляем этап, если он есть и тут
            try:
                stages = dict(await client.stages(current_cat))
            except (BitrixError, httpx.HTTPError) as ex:
                # Адрес рабочий (max_deal_id прошёл), а этапы не получили — сохраняем адрес, этап выберут позже
                changes["no_messenger_stage"] = ""
                msg = f"!Адрес сохранён, но Битрикс не отдал этапы воронки: {ex}. Обновите страницу и выберите этап"
            else:
                if s.no_messenger_stage not in stages:
                    changes["no_messenger_stage"] = ""
                    msg = "Битрикс подключён. Теперь выберите этап «Не нашёл мессенджер»"
        elif client and category is not None:
            if category != current_cat:
                changes["deal_category_id"] = category
                changes["no_messenger_stage"] = ""
                msg = "Воронка сменена — выберите этап «Не нашёл мессенджер» для неё"
            elif stage:
                try:
                    stages = dict(await client.stages(category))
                except (BitrixError, httpx.HTTPError) as ex:
                    return go("/settings#bitrix", f"!Битрикс не отдал этапы воронки: {ex}")
                if stage in stages:
                    changes["deal_category_id"] = category
                    changes["no_messenger_stage"] = stage
                else:
                    msg = "!Такого этапа в работе нет в выбранной воронке (закрывающий этап выбрать нельзя)"
            else:
                msg = "!Выберите этап «Не нашёл мессенджер»" if not s.no_messenger_stage else msg
        worker.save(**changes)
        worker.event(None, "настройки Битрикса изменены", user["email"])
        return go("/settings#bitrix", msg)

    @router.post("/settings/orbita")
    async def save_orbita(api_key: str = Form(""), webhook_secret: str = Form(""), webhook_secret_clear: str = Form(""),
                          place_whatsapp: str = Form(""), place_max: str = Form(""), place_telegram: str = Form(""),
                          user=Depends(admin_dep)):
        s = worker.s
        changes: dict = {}
        places = {"whatsapp": place_whatsapp, "max": place_max, "telegram": place_telegram}
        picked = {c: int(p) for c, p in places.items() if p.strip().isdigit()}
        if not picked:
            return go("/settings#orbita", "!Выберите хотя бы один мессенджер")
        if len(set(picked.values())) < len(picked):
            return go("/settings#orbita", "!У двух мессенджеров одно место в очереди — поставьте разные")
        chosen = sorted(picked, key=picked.get)
        changes["orbita_channels"] = chosen
        api_key = api_key.strip()
        if api_key:
            try:
                await OrbitaAPI(s.orbita_api_url, api_key, chosen).account()
            except Exception as ex:  # noqa: BLE001
                return go("/settings#orbita", f"!Орбита не приняла ключ: {ex}")
            changes["orbita_api_key"] = api_key
        if webhook_secret.strip():
            changes["orbita_webhook_secret"] = webhook_secret.strip()
        elif webhook_secret_clear:
            changes["orbita_webhook_secret"] = ""
        worker.save(**changes)
        worker.event(None, "настройки Орбиты изменены", user["email"])
        return go("/settings#orbita", "Сохранено")

    @router.post("/settings/messages")
    async def save_messages(tpl_first: str = Form(...), tpl_followup: str = Form(...), tpl_reply: str = Form(...),
                            tpl_no_name: str = Form(""), followup_on: str = Form(""), reply_on: str = Form(""),
                            followup_hours: str = Form("24"), reply_delay_minutes: str = Form("2"),
                            user=Depends(admin_dep)):
        texts = {"tpl_first": tpl_first, "tpl_followup": tpl_followup, "tpl_reply": tpl_reply}
        titles = {"tpl_first": "первом сообщении", "tpl_followup": "напоминании", "tpl_reply": "ответе клиенту"}
        for k, v in texts.items():
            v = v.replace("\r\n", "\n").strip()
            if not v:
                return go("/settings#messages", f"!Пустой текст в {titles[k]}")
            try:
                render(v, SAMPLE_NAME, SAMPLE_TITLE)
            except (KeyError, IndexError, ValueError) as ex:
                return go("/settings#messages", f"!Ошибка в {titles[k]}: {ex}. Доступно только {{name}} и {{deal_title}}")
            texts[k] = v
        try:   # «2,5» с запятой — тоже число
            followup_hours = float(followup_hours.replace(",", ".").strip())
            reply_delay_minutes = float(reply_delay_minutes.replace(",", ".").strip())
        except ValueError:
            return go("/settings#messages", "!Часы напоминания и паузу ответа укажите числом, например 24 или 2,5")
        if not (0.05 <= followup_hours <= 720) or not (0 <= reply_delay_minutes <= 60):
            return go("/settings#messages", "!Проверьте часы напоминания и паузу ответа")
        worker.save(**texts, tpl_no_name=tpl_no_name.strip() or "Здравствуйте",
                    followup_on=bool(followup_on), reply_on=bool(reply_on),
                    followup_hours=followup_hours, reply_delay_minutes=reply_delay_minutes)
        worker.event(None, "шаблоны изменены", user["email"])
        return go("/settings#messages", "Сохранено, действует для следующих сообщений")

    @router.post("/settings/schedule")
    async def save_schedule(start: str = Form(...), end: str = Form(...), tz: str = Form(...),
                            user=Depends(admin_dep)):
        if tz not in available_timezones():
            return go("/settings#schedule", "!Неизвестный часовой пояс")
        try:
            from datetime import time as dtime
            if dtime.fromisoformat(start) >= dtime.fromisoformat(end):
                return go("/settings#schedule", "!Начало окна должно быть раньше конца")
        except ValueError:
            return go("/settings#schedule", "!Время в формате ЧЧ:ММ")
        ZoneInfo(tz)
        worker.save(window_start=start[:5], window_end=end[:5], tz_name=tz)
        worker.event(None, "расписание изменено", f"{start}–{end} {tz}, {user['email']}")
        return go("/settings#schedule", "Сохранено")

    @router.post("/settings/bitrix/disconnect")
    async def disconnect_bitrix(user=Depends(admin_dep)):
        if worker.s.enabled:
            await worker.disable()
        worker.save(bitrix_webhook="", bitrix_app_token="", no_messenger_stage="", deal_category_id=None)
        worker.event(None, "Битрикс отключён", user["email"])
        return go("/settings#bitrix", "Битрикс отключён, робот выключен")

    @router.post("/settings/orbita/disconnect")
    async def disconnect_orbita(user=Depends(admin_dep)):
        if worker.s.enabled:
            await worker.disable()
        worker.save(orbita_api_key="", orbita_webhook_secret="")
        worker.event(None, "Орбита отключена", user["email"])
        return go("/settings#orbita", "Орбита отключена, робот выключен")

    # ---------- робот ----------

    @router.post("/settings/enable")
    async def enable(user=Depends(admin_dep)):
        if not worker.s.configured:
            return go("/settings#robot", "!Сначала подключите Битрикс (с этапом) и Орбиту")
        try:
            stages = dict(await worker.bitrix.stages(worker.s.category))
        except Exception as ex:  # noqa: BLE001
            return go("/settings#robot", f"!Не получилось включить: Битрикс не отдал этапы воронки ({ex})")
        if worker.s.no_messenger_stage not in stages:
            return go("/settings#bitrix", "!Робот не включён: этап «Не нашёл мессенджер» — не этап в работе этой "
                                          "воронки (закрывающий или удалён). Выберите этап в работе")
        try:
            start = await worker.enable()
        except Exception as ex:  # noqa: BLE001
            return go("/settings#robot", f"!Не получилось включить: Битрикс не ответил ({ex})")
        return go("/settings#robot", f"Робот включён. Сделки до ID {start} он не тронет.")

    @router.post("/settings/disable")
    async def disable(user=Depends(admin_dep)):
        await worker.disable()
        return go("/settings#robot", "Робот выключен")

    @router.post("/settings/test")
    async def test_send(phone: str = Form(...), name: str = Form(""), user=Depends(admin_dep)):
        p = normalize_phone(phone)
        if not p:
            return go("/settings#test", "!Не похоже на номер телефона")
        if worker.orbita is None:
            return go("/settings#test", "!Сначала подключите Орбиту")
        text = worker.render("first", name.strip(), SAMPLE_TITLE)
        if worker.dry_run:
            result = f"{p}: DRY_RUN — не отправлено (отправил бы: {text!r})"
            db.put("test_result", result)
            worker.event(None, "DRY_RUN: тест не отправлен", p)
            return go("/settings#test", "!" + result)
        try:
            res = await worker.orbita.send_first(p, text, f"test-{int(time.time())}-{p}", name.strip() or None)
        except Exception as ex:  # noqa: BLE001
            worker.event(None, "тест: ошибка", f"{p}: {ex}")
            return go("/settings#test", f"!Не отправлено: {ex}")
        if res.status == "no_messenger":
            result = f"{p}: номера нет ни в одном из выбранных мессенджеров"
            db.put("test_result", result)
            worker.event(None, "тест: мессенджер не найден", p)
            return go("/settings#test", "!" + result + ". Проверьте номер или отметьте другие мессенджеры.")
        result = (f"{p}: отправлено в {CHANNEL_RU.get(res.channel, res.channel)}" if res.status == "sent"
                  else f"{p}: передано в {CHANNEL_RU.get(res.channel, res.channel)}, ждём подтверждения")
        db.put("test_sent_at", int(time.time()))
        db.put("test_result", result)
        worker.event(None, "тест: сообщение отправлено", f"{p} {res.channel} ({user['email']})")
        return go("/settings#test", result + ". Проверьте телефон.")

    # ---------- команда ----------

    def team_section(user, new_link: str = "") -> str:
        rows = "".join(
            f'<tr><td>{e(u["email"])}</td><td>{e(u["name"])}</td><td>{ROLES[u["role"]]}</td>'
            f'<td class="num">{u["created_at"][:10]}</td><td>' +
            ("" if u["id"] == user["id"] else
             f'<form method="post" action="/team/{u["id"]}/delete" class="inline" '
             f'onsubmit="return confirm({e(json.dumps("Удалить " + u["email"] + "?", ensure_ascii=False))})">'
             '<button class="danger">Удалить</button></form>') +
            "</td></tr>" for u in users.all())
        invites = users.open_invites()
        inv = (f'<p class="hint">Неиспользованных приглашений: {len(invites)} '
               f'(истекают через 7 дней после выдачи).</p>') if invites else ""
        link = (f'<div class="card"><b>Ссылка-приглашение</b><p class="hint">Отправьте её человеку. Она показывается '
                f'один раз, действует 7 дней и только для одной учётной записи.</p><p><code>{e(new_link)}</code></p></div>'
                if new_link else "")
        return f"""<div class="card" id="team">
<h2 style="margin-top:0">Команда</h2><p class="hint">Кто может входить в панель. Администратор видит и меняет
настройки, оператор — только сделки и журнал.</p>{link}
<div class="wrap"><table><thead><tr><th scope="col">Почта</th><th scope="col">Имя</th><th scope="col">Роль</th><th scope="col">С</th><th scope="col">Действия</th></tr></thead><tbody>{rows}</tbody></table></div>
<h3>Пригласить</h3><form class="bar" method="post" action="/team/invite">
<label><span class="visually-hidden">Роль</span><select name="role"><option value="operator">оператор</option><option value="admin">администратор</option></select></label>
<button class="primary">Создать ссылку-приглашение</button></form>{inv}</div>"""

    @router.get("/team")
    async def team(user=Depends(admin_dep)):
        return go("/settings#team", "")   # старый адрес отдельной страницы

    @router.post("/team/invite", response_class=HTMLResponse)
    async def invite(request: Request, role: str = Form("operator"), user=Depends(admin_dep)):
        if role not in ROLES:
            return go("/settings#team", "!Неизвестная роль")
        token = users.new_invite(role, user["id"])
        worker.event(None, "выдано приглашение", f"{role}, {user['email']}")
        return await settings_page(request, user, "Ссылка-приглашение создана — она внизу, в разделе «Команда»",
                                   new_link=f"{ctx.public_url(request)}/invite/{token}")

    @router.post("/team/{user_id}/delete")
    async def delete_user(user_id: int, user=Depends(admin_dep)):
        target = users.by_id(user_id)
        if target is None or target["id"] == user["id"]:
            return go("/settings#team", "!Нельзя удалить себя")
        if target["role"] == "admin" and users.admins() <= 1:
            return go("/settings#team", "!Это последний администратор")
        users.delete(user_id)
        worker.event(None, "пользователь удалён", f"{target['email']} ({user['email']})")
        return go("/settings#team", f"{target['email']} удалён")

    return router
