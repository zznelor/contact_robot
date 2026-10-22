"""Общее для страниц панели: оформление, каркас страницы, проверка входа и ролей.

Страницы рендерятся на сервере, без JS-фреймворков и внешних файлов.
"""

import html
from datetime import datetime
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import SESSION_COOKIE, Users, same_origin
from .worker import Worker

STATUS_RU = {
    "new": "в очереди",
    "pending_first": "передано, ждём подтверждения",
    "sent_first": "написали, ждём ответа",
    "replied": "ответил",
    "sent_followup": "отправлено напоминание",
    "no_reply": "не ответил",
    "no_messenger": "нет мессенджера",
    "no_phone": "нет телефона",
    "skipped": "пропущена",
    "needs_check": "проверить вручную",
    "error": "ошибка",
}
# Тон статуса: ok — всё хорошо, wait — робот работает, warn — нужен человек, off — робот закончил без действия
TONE = {"new": "wait", "pending_first": "wait", "sent_first": "wait", "sent_followup": "wait",
        "replied": "ok", "no_reply": "off", "no_messenger": "warn", "no_phone": "warn",
        "needs_check": "warn", "error": "warn", "skipped": "off"}
# Порядок плиток на главной
ORDER = ["needs_check", "error", "no_messenger", "no_phone", "new", "pending_first",
         "sent_first", "sent_followup", "replied", "no_reply", "skipped"]


def e(v) -> str:
    return html.escape("" if v is None else str(v))


# Время в панели — в часовом поясе из настроек робота, а не сервера (в Docker это UTC)
_display_tz = lambda: None  # noqa: E731 — подменяется в Ctx


def when(iso: str | None) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(_display_tz()).strftime("%d.%m %H:%M")


def badge(status: str) -> str:
    return f'<span class="badge b-{TONE.get(status, "off")}">{e(STATUS_RU.get(status, status))}</span>'


def mask(secret: str) -> str:
    """Секрет в интерфейсе — только начало."""
    if not secret:
        return ""
    if len(secret) <= 3:
        return "…"                      # «начало» короткого ключа — весь ключ
    return secret[:10] + "…" if len(secret) > 14 else secret[:3] + "…"


def go(path: str, msg: str = "") -> RedirectResponse:
    """Переход после формы; msg — сообщение вверху страницы («!» в начале — ошибка)."""
    path, _, anchor = path.partition("#")
    if msg:
        path += ("&" if "?" in path else "?") + "msg=" + quote(msg)
    return RedirectResponse(path + (f"#{anchor}" if anchor else ""), status_code=303)


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1b1b1a;--muted:#6d6d69;--line:#e6e5e1;--accent:#2f5bd3;
--on-accent:#fff;--ok:#1f7a4a;--ok-bg:#e5f4ec;--wait:#2f5bd3;--wait-bg:#e8eefc;--warn:#a5450f;--warn-bg:#fcebdd;--off:#6d6d69;--off-bg:#efefec}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1c1c1b;--fg:#ececea;--muted:#9a9a95;--line:#2c2c2a;
--accent:#7c9cf5;--on-accent:#10162b;--ok:#6fd19c;--ok-bg:#183326;--wait:#93acf7;--wait-bg:#1d2540;--warn:#f2a36b;--warn-bg:#3a2415;--off:#9a9a95;--off-bg:#262624}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{background:var(--card);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}
header b{font-size:15px}nav{display:flex;gap:14px;flex-wrap:wrap}nav a{color:var(--muted)}nav a.on{color:var(--fg);font-weight:600}
header .me{margin-left:auto;display:flex;gap:10px;align-items:center;color:var(--muted);font-size:13px}
header .me button{padding:3px 10px;font-size:13px}
main{max-width:1180px;margin:0 auto;padding:20px 16px 48px}
main.narrow{max-width:440px;padding-top:56px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:24px 0 8px}h3{font-size:14px;margin:16px 0 6px}
.muted{color:var(--muted)}.small{font-size:12px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 0}
.chip{border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:12px;color:var(--muted);background:var(--card)}
.chip.alert{border-color:var(--warn);color:var(--warn)}.chip.good{border-color:var(--ok);color:var(--ok)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin:16px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;color:var(--fg)}
.tile:hover{text-decoration:none;border-color:var(--accent)}.tile.on{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.tile .n{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}.tile .l{font-size:12px;color:var(--muted)}
.tile.warn .n{color:var(--warn)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:0 0 16px}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
tr:last-child td{border-bottom:0}th{color:var(--muted);font-weight:500;font-size:12px;white-space:nowrap}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.badge{display:inline-block;border-radius:6px;padding:1px 8px;font-size:12px;white-space:nowrap}
.b-ok{color:var(--ok);background:var(--ok-bg)}.b-wait{color:var(--wait);background:var(--wait-bg)}
.b-warn{color:var(--warn);background:var(--warn-bg)}.b-off{color:var(--off);background:var(--off-bg)}
form.inline{display:inline}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 12px}
input[type=search],input[type=password],input[type=text],input[type=email],input[type=tel],input[type=number],input[type=time],select,textarea{
font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;max-width:100%}
.field{display:grid;gap:4px;margin:0 0 12px}.field>span{font-weight:500}.field input:not([type=checkbox]),.field select,.field textarea{width:100%}
.check{display:flex;gap:8px;align-items:flex-start;margin:0 0 8px}
textarea{width:100%;min-height:96px;font:14px/1.5 system-ui,-apple-system,sans-serif}
code{font:12px ui-monospace,Menlo,monospace;background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:1px 4px;word-break:break-all}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:6px 12px;cursor:pointer}
button:hover{border-color:var(--accent)}button.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
button.danger{color:var(--warn)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:0}dt{color:var(--muted)}dd{margin:0;word-break:break-word}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media (max-width:760px){.grid2{grid-template-columns:1fr}}
.preview{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;margin-top:8px}
.flash{border-radius:8px;padding:8px 12px;margin:0 0 12px;background:var(--ok-bg);color:var(--ok)}
.flash.err{background:var(--warn-bg);color:var(--warn)}
.ok{color:var(--ok)}.bad{color:var(--warn)}
.steps{list-style:none;padding:0;margin:8px 0 0;display:grid;gap:6px}.steps li{display:flex;gap:8px}
.steps .m{width:18px;text-align:center}
.toc{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0 16px}.toc a{border:1px solid var(--line);border-radius:999px;padding:3px 12px;background:var(--card);color:var(--fg)}
.hint{font-size:12px;color:var(--muted)}
.visually-hidden{position:absolute;width:1px;height:1px;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
"""

# Сохранение в настройках ждёт ответа Битрикса и Орбиты (1–3 с). Повторное нажатие обрывало запрос,
# и «Сохранено» не появлялось: после первого нажатия кнопка блокируется до ответа.
# Отправку, отменённую раньше (onsubmit="return confirm(…)" → «Отмена»), не блокируем: перехода не будет,
# и форма осталась бы «занятой» до перезагрузки страницы.
JS = """document.addEventListener("submit",function(ev){if(ev.defaultPrevented)return;var f=ev.target,b=ev.submitter;
if(f.dataset.busy){ev.preventDefault();return}f.dataset.busy="1";
if(b&&b.tagName==="BUTTON")setTimeout(function(){b.disabled=true;b.dataset.label=b.textContent;b.textContent="Подождите…"},0)});
window.addEventListener("pageshow",function(){document.querySelectorAll("form[data-busy]").forEach(function(f){delete f.dataset.busy});
document.querySelectorAll("button[data-label]").forEach(function(b){b.disabled=false;b.textContent=b.dataset.label;delete b.dataset.label})});"""


def shell(title: str, body: str, *, nav: str = "", me: str = "", narrow: bool = False) -> HTMLResponse:
    head = f'<header><b>Робот первого контакта</b><nav>{nav}</nav>{me}</header>' if nav or me else ""
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} · Первый контакт</title>
<meta name="referrer" content="same-origin">
<style>{CSS}</style></head><body>{head}
<main class="{'narrow' if narrow else ''}">{body}</main><script>{JS}</script></body></html>""")


def flash_html(msg: str) -> str:
    if not msg:
        return ""
    cls = "flash err" if msg.startswith("!") else "flash"
    return f'<div class="{cls}">{e(msg.lstrip("!"))}</div>'


class NeedLogin(Exception):
    """Не вошёл — на страницу входа (или создания администратора)."""


class Ctx:
    """Что нужно каждой странице: робот, пользователи, текущий пользователь."""

    def __init__(self, worker: Worker, users: Users):
        global _display_tz
        self.worker, self.users = worker, users
        _display_tz = lambda: worker.s.tz  # noqa: E731

    def page(self, user, title: str, active: str, body: str, msg: str = "") -> HTMLResponse:
        w, s = self.worker, self.worker.s
        chips, alerts = [], []
        if s.enabled:
            chips.append(('good', "робот включён"))
        else:
            alerts.append("робот выключен")
        chips.append(("", f"окно {s.window_start}–{s.window_end}"
                          f" · сейчас {'внутри' if w.in_window() else 'вне окна'}"))
        if s.followup_on:
            chips.append(("", f"напоминание через {s.followup_hours:g} ч"))
        if s.reply_on:
            chips.append(("", "ответ клиенту по шаблону"))
        if w.dry_run:
            alerts.append("DRY_RUN — ничего не отправляется")
        if s.orbita_api_key and not s.orbita_webhook_secret:
            alerts.append("вебхуки Орбиты не настроены")
        chip_html = "".join(f'<span class="chip {c}">{e(t)}</span>' for c, t in chips) + \
            "".join(f'<span class="chip alert">{e(t)}</span>' for t in alerts)
        links = [("/", "deals", "Сделки"), ("/events", "events", "Журнал"), ("/status", "status", "Проверка связи")]
        if user["role"] == "admin":
            links += [("/settings", "settings", "Настройки")]
        nav = "".join(f'<a href="{h}" class="{"on" if k == active else ""}">{t}</a>' for h, k, t in links)
        me = (f'<div class="me"><a href="/account">{e(user["email"])}</a>'
              f'<form method="post" action="/logout" class="inline"><button>Выйти</button></form></div>')
        return shell(title, f'<div class="chips">{chip_html}</div><div style="height:16px"></div>'
                            f'{flash_html(msg)}{body}', nav=nav, me=me)

    # ---------- зависимости FastAPI ----------

    def user_dep(self):
        users = self.users

        async def dep(request: Request):
            if request.method == "POST" and not same_origin(request):
                raise HTTPException(403, "Форма отправлена не со страницы панели")
            user = users.by_session(request.cookies.get(SESSION_COOKIE))
            if user is None:
                raise NeedLogin()
            return user
        return dep

    def admin_dep(self):
        user_dep = self.user_dep()

        async def dep(user=Depends(user_dep)):
            if user["role"] != "admin":
                raise HTTPException(403, "Нужны права администратора")
            return user
        return dep

    def public_url(self, request: Request) -> str:
        if self.worker.env.public_url:
            return self.worker.env.public_url
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        return f"{proto}://{host}"
