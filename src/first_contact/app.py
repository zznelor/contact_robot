"""HTTP: приём событий Битрикса и вебхуков Орбиты, панель (ui.py, ui_settings.py), фоновый цикл."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError

from .auth import Users, same_origin
from .orbita_api import verify_signature
from .ui import build_router
from .ui_base import Ctx, NeedLogin, e, go, shell
from .ui_settings import build_settings_router
from .worker import Worker

log = logging.getLogger("first_contact")


def create_app(worker: Worker) -> FastAPI:
    users = Users(worker.db)
    ctx = Ctx(worker, users)
    background: set[asyncio.Task] = set()   # ссылки на фоновые задачи, иначе их может собрать сборщик мусора

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)

    async def handle_orbita(ev: dict, event_id: str) -> None:
        try:
            await worker.on_orbita_event(ev)
        except Exception as ex:  # noqa: BLE001
            log.exception("вебхук Орбиты: ошибка обработки")
            worker.event(None, "Орбита: ошибка обработки события", f"{ev.get('type')} #{event_id}: {ex}")
            if event_id:
                worker.db.forget_event(event_id)   # повторная доставка обработается заново

    async def loop():
        while True:
            try:
                await worker.tick()
            except Exception:  # noqa: BLE001
                log.exception("цикл: ошибка")
            await asyncio.sleep(worker.env.poll_seconds)

    @asynccontextmanager
    async def lifespan(_app):
        if code := users.setup_code():
            log.warning("Администратора ещё нет. Откройте панель в браузере и введите "
                        "Код первого входа: %s", code)
        task = asyncio.create_task(loop())
        yield
        task.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(NeedLogin)
    async def need_login(_request, _exc):
        return go("/setup" if users.count() == 0 else "/login")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith(("/bitrix/", "/orbita/")):
            return Response(status_code=exc.status_code)
        resp = shell("Ошибка", f'<h1>{exc.status_code}</h1><p>{e(exc.detail)}</p><p><a href="/">На главную</a></p>',
                     narrow=True)
        resp.status_code = exc.status_code
        return resp

    @app.exception_handler(RequestValidationError)
    async def bad_form(request: Request, exc: RequestValidationError):
        if request.url.path.startswith(("/bitrix/", "/orbita/")):
            return Response(status_code=400)
        ref = request.headers.get("referer", "")
        back = ref if same_origin(request) and ref else "/"
        names = ", ".join(sorted({str(err.get("loc", ["", "?"])[-1]) for err in exc.errors()}))
        resp = shell("Ошибка", f'<h1>Проверьте форму</h1><p>Не заполнено или неверно: {e(names)}.</p>'
                               f'<p><a href="{e(back)}">← назад</a> · <a href="/">На главную</a></p>',
                     narrow=True)
        resp.status_code = 400
        return resp

    @app.post("/bitrix/deal-add")
    async def bitrix_deal_add(request: Request):
        # Битрикс шлёт form-urlencoded: event, data[FIELDS][ID], auth[application_token]
        s = worker.s
        form = await request.form()
        if not s.bitrix_app_token or form.get("auth[application_token]") != s.bitrix_app_token:
            return Response(status_code=403)
        if form.get("event", "").upper() != "ONCRMDEALADD":
            return {"ok": True, "ignored": form.get("event")}
        raw_id = str(form.get("data[FIELDS][ID]", "")).strip()
        if not raw_id.isdigit():
            return Response(status_code=400)
        if deal_id := int(raw_id):
            spawn(worker.on_deal_added(deal_id))
        return {"ok": True}

    @app.post("/orbita/webhook")
    async def orbita_webhook(request: Request):
        # Подпись: X-Orbita-Signature = v1=<HMAC-SHA256(секрет, "<X-Orbita-Timestamp>.<тело>")>
        body = await request.body()
        h = request.headers
        if not verify_signature(worker.s.orbita_webhook_secret, h.get("x-orbita-timestamp", ""), body,
                                h.get("x-orbita-signature", "")):
            return Response(status_code=401)
        try:
            ev = json.loads(body)
        except ValueError:
            return Response(status_code=400)
        if not isinstance(ev, dict):
            return Response(status_code=400)
        event_id = h.get("x-orbita-event-id") or str(ev.get("id") or "")
        # Без номера дубль не распознать — обрабатываем (иначе все такие события после первого пропали бы)
        if event_id and not worker.db.first_seen_event(event_id, ev.get("type", "")):
            return {"ok": True, "duplicate": True}
        # Отвечаем сразу, обработка — в фоне: Орбита ждёт 2xx, а у воркера может быть занят замок
        spawn(handle_orbita(ev, event_id))
        return {"ok": True}

    @app.get("/health")
    async def health():
        # Старый адрес проверки из первых версий образа; новые — /healthz и /ready
        return {"ok": True, "enabled": worker.s.enabled}

    @app.get("/healthz")
    async def healthz():
        """Liveness: процесс отвечает. Зависимости не проверяет — иначе сбой базы перезапускал бы сервис."""
        return {"ok": True}

    @app.get("/ready")
    async def ready():
        """Readiness: база открывается и читается. Битрикс и Орбита — внешние сервисы, их сбой не делает
        робота неготовым: сделки дождутся в очереди, а панель должна открываться."""
        try:
            worker.db.conn.execute("SELECT 1 FROM deals LIMIT 1").fetchall()
        except Exception as ex:  # noqa: BLE001
            log.error("ready: база недоступна: %s", ex)
            return Response(status_code=503)
        return {"ok": True}

    app.include_router(build_router(ctx))
    app.include_router(build_settings_router(ctx))
    return app
