"""Панель: вход, сделки, карточка сделки, журнал, проверка связи.

Настройки и команда — ui_settings.py. Вебхуки (/bitrix/…, /orbita/…) и /health под вход
не попадают: у них своя проверка.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from .auth import (MIN_PASSWORD, SESSION_COOKIE, SESSION_DAYS, RateLimit, email_problem, password_problem,
                   same_origin)
from .ui_base import (ORDER, STATUS_RU, TONE, Ctx, badge, e, flash_html, go, shell, when)
from .config import CHANNEL_RU
from .worker import RETRYABLE


def _set_session(resp, request: Request, token: str) -> None:
    secure = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=secure)


def build_router(ctx: Ctx) -> APIRouter:
    worker, users = ctx.worker, ctx.users
    db = worker.db
    limiter = RateLimit()
    router = APIRouter()
    user_dep = ctx.user_dep()

    def client_ip(request: Request) -> str:
        # За Caddy uvicorn сам подставляет адрес клиента из X-Forwarded-For (proxy_headers)
        return request.client.host if request.client else "?"

    # ---------- вход ----------

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(msg: str = ""):
        if users.count() == 0:
            return go("/setup")
        return shell("Вход", f"""<h1>Вход</h1><p class="muted">Робот первого контакта</p>{flash_html(msg)}
<form method="post" action="/login" class="card">
<label class="field"><span>Почта</span><input type="email" name="email" required autofocus autocomplete="username"></label>
<label class="field"><span>Пароль</span><input type="password" name="password" required autocomplete="current-password"></label>
<button class="primary">Войти</button></form>
<p class="hint">Забыли пароль — попросите администратора панели выдать новое приглашение, или администратор
сервера выполнит <code>reset-password</code> (см. docs/УСТАНОВКА.md).</p>""", narrow=True)

    @router.post("/login")
    async def login(request: Request, email: str = Form(...), password: str = Form(...)):
        if not same_origin(request):
            return go("/login", "!Обновите страницу и попробуйте снова")
        ip = client_ip(request)
        if limiter.blocked(ip):
            return go("/login", "!Слишком много попыток. Подождите 15 минут.")
        user = users.login(email, password)
        if user is None:
            limiter.fail(ip)
            return go("/login", "!Неверная почта или пароль")
        resp = go("/")
        _set_session(resp, request, users.new_session(user["id"]))
        return resp

    @router.post("/logout")
    async def logout(request: Request):
        if same_origin(request):
            users.end_session(request.cookies.get(SESSION_COOKIE))
        resp = go("/login")
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # ---------- первый администратор ----------

    @router.get("/setup", response_class=HTMLResponse)
    async def setup_page(msg: str = ""):
        if users.count():
            return go("/login")
        users.setup_code()
        return shell("Первый запуск", f"""<h1>Первый запуск</h1>
<p class="muted">Создайте администратора панели. Код подтверждения напечатан в логе сервиса:
<code>docker compose logs app | grep "Код первого входа"</code></p>{flash_html(msg)}
<form method="post" action="/setup" class="card">
<label class="field"><span>Код из лога</span><input type="text" name="code" required autocomplete="off"></label>
<label class="field"><span>Ваше имя</span><input type="text" name="name"></label>
<label class="field"><span>Почта</span><input type="email" name="email" required autocomplete="username"></label>
<label class="field"><span>Пароль</span><input type="password" name="password" required minlength="{MIN_PASSWORD}" autocomplete="new-password">
<span class="hint">Не короче {MIN_PASSWORD} символов</span></label>
<button class="primary">Создать и войти</button></form>""", narrow=True)

    @router.post("/setup")
    async def setup(request: Request, code: str = Form(...), email: str = Form(...),
                    password: str = Form(...), name: str = Form("")):
        if users.count():
            return go("/login")
        if not same_origin(request):
            return go("/setup", "!Обновите страницу и попробуйте снова")
        ip = client_ip(request)
        if limiter.blocked(ip):
            return go("/setup", "!Слишком много попыток. Подождите 15 минут.")
        if code.strip() != users.setup_code():
            limiter.fail(ip)
            return go("/setup", "!Код не подходит — возьмите его из лога сервиса")
        if problem := email_problem(email) or password_problem(password):
            return go("/setup", f"!{problem}")
        uid = users.create(email, password, "admin", name)
        users.setup_code()                       # код больше не действует
        worker.event(None, "создан администратор", email)
        resp = go("/settings", "Администратор создан. Теперь подключите Битрикс и Орбиту.")
        _set_session(resp, request, users.new_session(uid))
        return resp

    # ---------- приглашение ----------

    @router.get("/invite/{token}", response_class=HTMLResponse)
    async def invite_page(token: str, msg: str = ""):
        inv = users.invite(token)
        if inv is None:
            return shell("Приглашение", "<h1>Приглашение недействительно</h1><p class='muted'>Оно уже использовано "
                         "или истекло (7 дней). Попросите администратора прислать новое.</p>", narrow=True)
        return shell("Приглашение", f"""<h1>Добро пожаловать</h1>
<p class="muted">Вас пригласили в панель робота первого контакта.</p>{flash_html(msg)}
<form method="post" action="/invite/{e(token)}" class="card">
<label class="field"><span>Ваше имя</span><input type="text" name="name"></label>
<label class="field"><span>Почта</span><input type="email" name="email" required autocomplete="username"></label>
<label class="field"><span>Пароль</span><input type="password" name="password" required minlength="{MIN_PASSWORD}" autocomplete="new-password">
<span class="hint">Не короче {MIN_PASSWORD} символов</span></label>
<button class="primary">Создать учётную запись</button></form>""", narrow=True)

    @router.post("/invite/{token}")
    async def invite_accept(request: Request, token: str, email: str = Form(...), password: str = Form(...),
                            name: str = Form("")):
        if not same_origin(request):
            return go(f"/invite/{token}", "!Обновите страницу и попробуйте снова")
        inv = users.invite(token)
        if inv is None:
            return go(f"/invite/{token}")
        if problem := email_problem(email) or password_problem(password):
            return go(f"/invite/{token}", f"!{problem}")
        if users.by_email(email):
            return go(f"/invite/{token}", "!Пользователь с такой почтой уже есть — просто войдите")
        uid = users.create(email, password, inv["role"], name)
        users.use_invite(token)
        worker.event(None, "новый пользователь по приглашению", f"{email} ({inv['role']})")
        resp = go("/")
        _set_session(resp, request, users.new_session(uid))
        return resp

    # ---------- своя учётная запись ----------

    @router.get("/account", response_class=HTMLResponse)
    async def account(msg: str = "", user=Depends(user_dep)):
        body = f"""<h1>Учётная запись</h1><div class="grid2"><div class="card"><dl>
<dt>Почта</dt><dd>{e(user['email'])}</dd><dt>Имя</dt><dd>{e(user['name']) or '—'}</dd>
<dt>Роль</dt><dd>{'администратор' if user['role'] == 'admin' else 'оператор'}</dd></dl></div>
<form class="card" method="post" action="/account/password"><b>Сменить пароль</b><div style="height:10px"></div>
<label class="field"><span>Текущий пароль</span><input type="password" name="old" required autocomplete="current-password"></label>
<label class="field"><span>Новый пароль</span><input type="password" name="new" required minlength="{MIN_PASSWORD}" autocomplete="new-password"></label>
<button class="primary">Сменить</button><p class="hint">После смены нужно будет войти заново на всех устройствах.</p></form></div>"""
        return ctx.page(user, "Учётная запись", "", body, msg)

    @router.post("/account/password")
    async def change_password(old: str = Form(...), new: str = Form(...), user=Depends(user_dep)):
        if users.login(user["email"], old) is None:
            return go("/account", "!Текущий пароль не подходит")
        if problem := password_problem(new):
            return go("/account", f"!{problem}")
        users.set_password(user["id"], new)
        return go("/login", "Пароль изменён, войдите с новым")

    # ---------- сделки ----------

    def onboarding(user) -> str:
        s = worker.s
        if s.enabled:
            return ""
        if user["role"] != "admin":
            return ('<div class="card"><b>Робот выключен.</b> <span class="muted">Включить его может '
                    'администратор в «Настройках».</span></div>')
        steps = [(s.bitrix_ok, "Подключить Битрикс", "/settings#bitrix"),
                 (s.orbita_ok, "Подключить Орбиту", "/settings#orbita"),
                 (bool(db.get("test_sent_at")), "Отправить тестовое сообщение себе", "/settings#robot"),
                 (False, "Включить робота", "/settings#robot")]
        lis = "".join(f'<li><span class="m {"ok" if done else "muted"}">{"✓" if done else "○"}</span>'
                      f'<a href="{href}">{e(t)}</a></li>' for done, t, href in steps)
        return f'<div class="card"><b>Робот ещё не работает.</b> Осталось:<ol class="steps">{lis}</ol></div>'

    @router.get("/", response_class=HTMLResponse)
    async def deals(status: str = "", q: str = "", msg: str = "", user=Depends(user_dep)):
        counts = db.counts()
        total = sum(counts.values())
        tiles = [f'<a class="tile {"on" if not status else ""}" href="/"><div class="n">{total}</div>'
                 f'<div class="l">все заявки</div></a>']
        for st in ORDER:
            if counts.get(st):
                warn = "warn" if TONE.get(st) == "warn" else ""
                tiles.append(f'<a class="tile {warn} {"on" if st == status else ""}" href="/?status={st}">'
                             f'<div class="n">{counts[st]}</div><div class="l">{e(STATUS_RU[st])}</div></a>')
        rows, found = db.find(status, q, limit=300)
        trs = "".join(
            f'<tr><td class="num"><a href="/deal/{r["deal_id"]}">{r["deal_id"]}</a></td>'
            f'<td>{e(r["name"])}</td><td class="num">{e(r["phone"])}</td><td>{badge(r["status"])}</td>'
            f'<td>{e(r["channel"])}</td><td class="num">{when(r["first_sent_at"])}</td>'
            f'<td class="num">{when(r["followup_sent_at"])}</td>'
            f'<td class="small muted">{e(r["last_error"])}</td></tr>'
            for r in rows
        ) or '<tr><td colspan="8" class="muted">Заявок пока нет</td></tr>'
        body = f"""{onboarding(user)}<h1>Заявки</h1>
<p class="muted">Новые сделки Битрикса, которым робот пишет первым. Оранжевым — где нужен человек.</p>
<div class="tiles">{''.join(tiles)}</div>
<div class="bar"><form class="bar" style="margin:0" method="get"><input type="hidden" name="status" value="{e(status)}">
<label><span class="visually-hidden">Поиск заявки</span><input type="search" name="q" value="{e(q)}" placeholder="ID, имя или телефон"></label><button>Найти</button></form>
<form class="bar" style="margin:0" method="post" action="/actions/tick">
<label><span class="visually-hidden">Действие робота (робот и так делает всё это сам раз в минуту)</span>
<select name="step"><option value="all">Всё сразу (как раз в минуту)</option>
{''.join(f'<option value="{k}">{e(v)}</option>' for k, v in worker.STEPS.items())}</select></label>
<button>Выполнить сейчас</button></form></div>
<div class="wrap"><table><thead><tr><th scope="col">Сделка</th><th scope="col">Имя</th><th scope="col">Телефон</th><th scope="col">Статус</th><th scope="col">Канал</th>
<th scope="col">Первое</th><th scope="col">Напоминание</th><th scope="col">Комментарий</th></tr></thead><tbody>{trs}</tbody></table></div>
<p class="small muted">Показано {len(rows)} из {found}.</p>"""
        return ctx.page(user, "Заявки", "deals", body, msg)

    @router.get("/deal/{deal_id}", response_class=HTMLResponse)
    async def deal(deal_id: int, msg: str = "", user=Depends(user_dep)):
        r = db.deal(deal_id)
        if r is None:
            return ctx.page(user, "Нет сделки", "deals", f"<h1>Сделки {deal_id} нет в базе робота</h1>"
                            f'<p><a href="/">← к списку</a></p>'
                            f'<form method="post" action="/deal/{deal_id}/add"><button class="primary">'
                            f"Взять в работу</button></form>"
                            f'<p class="small muted">Робот напишет клиенту по этой сделке, как по новой.</p>', msg)
        fields = [("Статус", badge(r["status"])), ("Имя", e(r["name"])), ("Телефон", e(r["phone"])),
                  ("Канал", e(r["channel"])), ("Диалог Орбиты", e(r["dialog_id"])),
                  ("Первое сообщение", when(r["first_sent_at"])),
                  ("Напоминание запланировано", when(r["followup_due_at"]) if r["status"] == "sent_first" else ""),
                  ("Напоминание отправлено", when(r["followup_sent_at"]) +
                   (" (передано, ждём подтверждения мессенджера)" if r["followup_request_key"] else "")),
                  ("Клиент ответил", when(r["client_replied_at"])),
                  ("Ответ по шаблону", (when(r["reply_sent_at"]) +
                                        (" (передан, ждём подтверждения мессенджера)" if r["reply_request_key"] else ""))
                   if r["reply_sent_at"] else
                   (f"запланирован на {when(r['reply_due_at'])}" if r["reply_due_at"] and r["status"] == "replied"
                    else "не отправлен — «Повторить» отправит" if r["reply_due_at"] else "")),
                  ("Попыток с ошибкой", str(r["attempts"] or 0)),
                  ("Следующая попытка", when(r["next_attempt_at"]) if r["status"] not in ("error", "skipped") else ""),
                  ("Комментарий", e(r["last_error"])),
                  ("Увидели заявку", when(r["created_at"])), ("Обновлено", when(r["updated_at"]))]
        dl = "".join(f"<dt>{k}</dt><dd>{v or '—'}</dd>" for k, v in fields)
        actions = []
        if r["status"] in RETRYABLE:
            hint = ("Робот продолжит с того места, где остановился" if r["first_sent_at"] else
                    "Вернуть в очередь: робот ещё раз попробует написать первым")
            if r["status"] == "needs_check" and not r["first_sent_at"]:
                hint = ("Сначала откройте переписку в Орбите: если первое сообщение дошло, "
                        "повтор отправит его второй раз")
            actions.append(_btn(deal_id, "retry", "Повторить", "primary", hint))
        if r["status"] in ("sent_first", "sent_followup", "needs_check", "pending_first", "no_reply"):
            actions.append(_btn(deal_id, "replied", "Клиент ответил", "",
                                "Робот больше не пишет, ответ по шаблону не отправляется"))
        if r["status"] in ("new", "sent_first", "sent_followup", "pending_first", "error", "needs_check") \
                or r["reply_due_at"]:
            actions.append(_btn(deal_id, "stop", "Остановить робота", "danger",
                                "Робот больше ничего не делает по этой сделке"))
        evs = "".join(f'<tr><td class="num">{when(ev["at"])}</td><td>{e(ev["event"])}</td>'
                      f'<td class="small muted">{e(ev["detail"])}</td></tr>' for ev in db.events(deal_id)) \
            or '<tr><td colspan="3" class="muted">Событий нет</td></tr>'
        first = f'<h3>Первое сообщение</h3><div class="preview">{e(r["first_text"])}</div>' if r["first_text"] else ""
        portal = worker.s.portal
        link = (f'<a href="{e(portal)}/crm/deal/details/{deal_id}/" target="_blank" rel="noopener">'
                f'открыть в Битриксе ↗</a>') if portal else ""
        body = f"""<p><a href="/">← все заявки</a></p><h1>Сделка {deal_id}</h1>
<p class="muted">{link}</p>
<div class="grid2"><div class="card"><dl>{dl}</dl></div>
<div class="card"><b>Действия</b><div style="margin-top:10px;display:grid;gap:10px">{''.join(actions) or '<span class="muted">Робот закончил с этой сделкой.</span>'}</div>{first}</div></div>
<h2>Хронология</h2><div class="wrap"><table><thead><tr><th scope="col">Когда</th><th scope="col">Событие</th><th scope="col">Подробности</th></tr></thead><tbody>{evs}</tbody></table></div>"""
        return ctx.page(user, f"Сделка {deal_id}", "deals", body, msg)

    def back(deal_id: int, msg: str):
        return go(f"/deal/{deal_id}", msg)

    @router.post("/deal/{deal_id}/retry")
    async def retry(deal_id: int, user=Depends(user_dep)):
        if not worker.s.enabled:
            return back(deal_id, "!Робот выключен — сначала включите его в настройках")
        try:
            status = await worker.retry(deal_id, user["email"])
        except ValueError:
            return back(deal_id, "!Эту сделку сейчас нельзя повторить")
        if status == "new":
            await worker.on_deal_added(deal_id)
        r = db.deal(deal_id)
        return back(deal_id, f"Готово. Статус: {STATUS_RU.get(r['status'], r['status'])}")

    @router.post("/deal/{deal_id}/replied")
    async def replied(deal_id: int, user=Depends(user_dep)):
        if not await worker.mark_replied(deal_id, user["email"]):
            return back(deal_id, "!Сделки нет в базе робота")
        return back(deal_id, "Отмечено: клиент ответил, робот больше не пишет")

    @router.post("/deal/{deal_id}/stop")
    async def stop(deal_id: int, user=Depends(user_dep)):
        if not await worker.stop(deal_id, user["email"]):
            return back(deal_id, "!Сделки нет в базе робота")
        return back(deal_id, "Робот остановлен по этой сделке")

    @router.post("/deal/{deal_id}/add")
    async def add(deal_id: int, user=Depends(user_dep)):
        if not worker.s.enabled:
            return back(deal_id, "!Робот выключен — сначала включите его в настройках")
        await worker.take(deal_id, user["email"])
        r = db.deal(deal_id)
        if r is None:
            return back(deal_id, "!Робот выключен или не настроен")
        return back(deal_id, f"Статус: {STATUS_RU.get(r['status'], r['status'])}")

    @router.post("/actions/tick")
    async def tick(step: str = Form("all"), user=Depends(user_dep)):
        if not worker.s.enabled:
            return go("/", "!Робот выключен")
        if not worker.s.configured:
            return go("/", "!Робот не настроен: подключите Битрикс (с этапом) и Орбиту в «Настройках»")
        if step != "all" and step not in worker.STEPS:
            return go("/", "!Неизвестное действие")
        try:
            if step == "all":
                await worker.tick()
            else:
                await worker.run_step(step)
            msg = f"Готово: {worker.STEPS.get(step, 'весь проход').lower()}"
            worker.event(None, f"вручную: {worker.STEPS.get(step, 'весь проход').lower()}", user["email"])
        except Exception as ex:  # noqa: BLE001
            msg = f"!Ошибка: {type(ex).__name__}: {ex}"
        return go("/", msg)

    # ---------- журнал ----------

    @router.get("/events", response_class=HTMLResponse)
    async def events(user=Depends(user_dep)):
        trs = "".join(
            f'<tr><td class="num">{when(ev["at"])}</td>'
            f'<td class="num">{f"""<a href="/deal/{ev["deal_id"]}">{ev["deal_id"]}</a>""" if ev["deal_id"] else "—"}</td>'
            f'<td>{e(ev["event"])}</td><td class="small muted">{e(ev["detail"])}</td></tr>'
            for ev in db.events(limit=500)
        ) or '<tr><td colspan="4" class="muted">Событий нет</td></tr>'
        body = f"""<h1>Журнал</h1><p class="muted">Последние 500 событий робота, новые сверху.</p>
<div class="wrap"><table><thead><tr><th scope="col">Когда</th><th scope="col">Сделка</th><th scope="col">Событие</th><th scope="col">Подробности</th></tr></thead><tbody>{trs}</tbody></table></div>"""
        return ctx.page(user, "Журнал", "events", body)

    # ---------- проверка связи ----------

    @router.get("/status", response_class=HTMLResponse)
    async def status(request: Request, user=Depends(user_dep)):
        s = worker.s
        rows = []

        async def probe(name, coro):
            try:
                rows.append((name, True, await coro))
            except Exception as ex:  # noqa: BLE001
                rows.append((name, False, f"{type(ex).__name__}: {ex}"))

        async def bitrix_info():
            stages = dict(await worker.bitrix.stages(s.category))
            has = s.no_messenger_stage in stages
            return (f"последняя сделка {await worker.bitrix.max_deal_id()}; этап «Не нашёл мессенджер»: "
                    f"{stages.get(s.no_messenger_stage) if has else 'НЕ НАЙДЕН — выберите в настройках'}")

        async def orbita_info():
            acc = await worker.orbita.account()
            return (f"аккаунт «{acc.get('name')}», подписка "
                    f"{'активна' if acc.get('subscription_active') else 'НЕАКТИВНА'}")

        async def channels_info():
            chs = await worker.orbita.channel_status()
            return ", ".join(f"{CHANNEL_RU.get(c, c)} — "
                             f"{'подключён' if ok else 'не подключён' if ok is None else 'отключился'}"
                             for c, ok in chs.items())

        if worker.bitrix:
            await probe("Битрикс", bitrix_info())
        else:
            rows.append(("Битрикс", False, "не подключён"))
        if worker.orbita:
            await probe("Орбита", orbita_info())
            await probe("Каналы Орбиты", channels_info())
        else:
            rows.append(("Орбита", False, "не подключена"))
        last_ev = next((ev for ev in db.events(limit=500) if ev["event"].startswith("Орбита:")), None)
        rows.append(("Вебхуки Орбиты", bool(s.orbita_webhook_secret),
                     ("секрет задан" if s.orbita_webhook_secret else "не настроены — ответы клиентов видны с задержкой") +
                     (f"; последнее служебное событие {when(last_ev['at'])}: {last_ev['event']}" if last_ev else "")))
        rows.append(("События Битрикса", bool(s.bitrix_app_token),
                     "токен задан" if s.bitrix_app_token else "не настроены — новые сделки видны с задержкой до минуты"))
        trs = "".join(f'<tr><td>{e(n)}</td><td class="{"ok" if ok else "bad"}">{"✓" if ok else "✕"}</td>'
                      f"<td>{e(d)}</td></tr>" for n, ok, d in rows)
        base = ctx.public_url(request)
        body = f"""<h1>Проверка связи</h1><p class="muted">Живой запрос к Битриксу и Орбите при открытии страницы.</p>
<div class="wrap"><table><thead><tr><th scope="col">Что</th><th scope="col">Состояние</th><th scope="col">Результат</th></tr></thead><tbody>{trs}</tbody></table></div>
<h2>Адреса для настройки</h2><div class="card"><dl>
<dt>Битрикс → исходящий вебхук «Создание сделки»</dt><dd><code>{e(base)}/bitrix/deal-add</code></dd>
<dt>Орбита → Разработчикам → Вебхуки</dt><dd><code>{e(base)}/orbita/webhook</code></dd></dl></div>"""
        return ctx.page(user, "Проверка связи", "status", body)

    return router


def _btn(deal_id: int, action: str, label: str, cls: str, hint: str) -> str:
    return (f'<form method="post" action="/deal/{deal_id}/{action}"><button class="{cls}">{e(label)}</button>'
            f' <span class="small muted">{e(hint)}</span></form>')
