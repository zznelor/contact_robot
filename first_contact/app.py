"""HTTP: приём событий Битрикса и вебхуков Орбиты, панель (ui.py, ui_settings.py), фоновый цикл."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response

from .auth import Users
from .orbita_api import verify_signature
from .ui import build_router
from .ui_base import Ctx, NeedLogin, e, go, shell
from .ui_settings import build_settings_router
from .worker import Worker

log = logging.getLogger("first_contact")


def create_app(worker: Worker) -> FastAPI:
    users = Users(worker.db)
    ctx = Ctx(worker, users)

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

    @app.post("/bitrix/deal-add")
    async def bitrix_deal_add(request: Request):
        # Битрикс шлёт form-urlencoded: event, data[FIELDS][ID], auth[application_token]
        s = worker.s
        form = await request.form()
        if not s.bitrix_app_token or form.get("auth[application_token]") != s.bitrix_app_token:
            return Response(status_code=403)
        if form.get("event", "").upper() != "ONCRMDEALADD":
            return {"ok": True, "ignored": form.get("event")}
        deal_id = int(form.get("data[FIELDS][ID]", 0))
        if deal_id:
            asyncio.create_task(worker.on_deal_added(deal_id))
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
        event_id = h.get("x-orbita-event-id") or str(ev.get("id", ""))
        if not worker.db.first_seen_event(event_id, ev.get("type", "")):
            return {"ok": True, "duplicate": True}
        # Отвечаем сразу, обработка — в фоне: Орбита ждёт 2xx, а у воркера может быть занят замок
        asyncio.create_task(worker.on_orbita_event(ev))
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True, "enabled": worker.s.enabled}

    app.include_router(build_router(ctx))
    app.include_router(build_settings_router(ctx))
    return app
