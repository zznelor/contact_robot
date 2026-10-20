"""Запуск (в Docker — `docker compose exec app python -m first_contact <команда>`):

  python -m first_contact run [порт]           — сервер с панелью и фоновым циклом (по умолчанию 8080)
  python -m first_contact check                — проверить связь с Битриксом и Орбитой
  python -m first_contact once                 — один проход очереди
  python -m first_contact process ID           — обработать конкретную сделку сейчас
  python -m first_contact setup-code           — код для создания первого администратора
  python -m first_contact create-admin ПОЧТА   — создать администратора (пароль спросит)
  python -m first_contact reset-password ПОЧТА — задать новый пароль пользователю
  python -m first_contact backup               — копия базы рядом с ней: backup-ГГГГ-ММ-ДД.sqlite3
  python -m first_contact restore ФАЙЛ         — заменить базу копией (потом перезапустить сервис)
"""

import asyncio
import getpass
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

from . import config
from .auth import Users, password_problem
from .db import DB
from .worker import Worker


def build() -> Worker:
    env = config.env()
    db = DB(env.db_path)
    return Worker(env, config.load(db), db)


async def check(w: Worker) -> None:
    s = w.s
    print(f"Робот: {'включён' if s.enabled else 'выключен'}; база {w.env.db_path}")
    print("Битрикс:")
    if not w.bitrix:
        print("  не подключён (Настройки → Битрикс)")
    else:
        cat = s.deal_category_id if s.deal_category_id is not None else "0"
        stages = dict(await w.bitrix.stages(cat))
        print(f"  {s.portal}, воронка {cat}, этапов {len(stages)}")
        print(f"  этап «Не нашёл мессенджер»: "
              f"{stages.get(s.no_messenger_stage) or 'НЕ ВЫБРАН — Настройки → Битрикс'}")
        print(f"  последняя сделка: ID {await w.bitrix.max_deal_id()}")
    print("Орбита:")
    if not w.orbita:
        print("  не подключена (Настройки → Орбита)")
    else:
        acc = await w.orbita.account()
        print(f"  аккаунт «{acc.get('name')}», тариф {acc.get('plan')},"
              f" подписка {'активна' if acc.get('subscription_active') else 'НЕАКТИВНА'}")
        for ch in await w.orbita.channels():
            print(f"  {ch['канал']}: {'подключён' if ch['подключён'] else 'не подключён'}")
        print(f"  порядок каналов: {', '.join(s.orbita_channels)}")
        print(f"  вебхуки: {'секрет задан' if s.orbita_webhook_secret else 'не настроены'}")
    print(f"Окно отправки: {s.window_start}–{s.window_end} ({s.tz_name}),"
          f" сейчас {'внутри' if w.in_window() else 'вне окна'}")


def _ask_password() -> str:
    while True:
        p = getpass.getpass("Пароль: ")
        if problem := password_problem(p):
            print(problem)
            continue
        if getpass.getpass("Ещё раз: ") != p:
            print("Не совпадает")
            continue
        return p


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%d.%m %H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе в лог попадает адрес вебхука с секретом
    args = sys.argv[1:] or ["run"]
    cmd = args[0]
    w = build()
    users = Users(w.db)
    if cmd == "check":
        asyncio.run(check(w))
    elif cmd == "once":
        asyncio.run(w.tick())
    elif cmd == "process":
        deal_id = int(args[1])
        w.db.add_deal(deal_id)
        asyncio.run(w._guarded(deal_id, w.first_contact))
        print(dict(w.db.deal(deal_id)))
    elif cmd == "setup-code":
        code = users.setup_code()
        print(f"Код первого входа: {code}" if code else "Администратор уже создан — код не нужен.")
    elif cmd == "create-admin" and len(args) > 1:
        if users.by_email(args[1]):
            print("Такой пользователь уже есть. Новый пароль: reset-password")
            sys.exit(1)
        users.create(args[1], _ask_password(), "admin")
        users.setup_code()
        print(f"Администратор {args[1]} создан.")
    elif cmd == "reset-password" and len(args) > 1:
        u = users.by_email(args[1])
        if u is None:
            print("Нет такого пользователя. Есть: " + ", ".join(x["email"] for x in users.all()))
            sys.exit(1)
        users.set_password(u["id"], _ask_password())
        print("Пароль изменён, все сессии пользователя закрыты.")
    elif cmd == "backup":
        target = w.env.db_path.with_name(f"backup-{date.today():%Y-%m-%d}.sqlite3")
        with sqlite3.connect(target) as dst:
            w.db.conn.backup(dst)           # корректная копия даже во время работы сервиса
        print(f"Копия: {target}")
    elif cmd == "restore" and len(args) > 1:
        src = Path(args[1])
        if not src.exists():
            print(f"Нет файла {src}")
            sys.exit(1)
        with sqlite3.connect(src) as source:
            source.backup(w.db.conn)
        print("База заменена копией. Перезапустите сервис: docker compose restart app")
    elif cmd == "run":
        import uvicorn
        from .app import create_app
        port = int(args[1]) if len(args) > 1 else w.env.port
        # proxy_headers: за Caddy адрес клиента и https берём из X-Forwarded-*
        uvicorn.run(create_app(w), host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")
    else:
        print(__doc__)


main()
