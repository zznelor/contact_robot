# Робот первого контакта

Python 3.11+ (FastAPI, httpx, SQLite). Пишет клиенту в мессенджер через Орбиту по новой сделке Битрикс24.
Устройство кода, статусы сделки, защита от дублей — `docs/РАЗРАБОТЧИКУ.md`.

## Где что

| Путь | Что |
|---|---|
| `src/first_contact/` | весь код сервиса (пакет, ставится `uv sync`) |
| `tests/` | тесты pytest |
| `scripts/` | служебные скрипты (smoke образа) |
| `docs/` | `УСТАНОВКА.md` (клиенту), `НАСТРОЙКА.md` (владельцу), `РАЗРАБОТЧИКУ.md`, копия OpenAPI Орбиты |
| `Dockerfile`, `Caddyfile` | образ и HTTPS-прокси |
| `docker-compose.yml` | **клиентская установка** (приложение + Caddy); имя не менять — по нему работают инструкции и уже установленные копии |
| `docker-compose.local.yml` | разработка: сборка из исходников, без Caddy, `DRY_RUN=1` |

## Запуск

```bash
uv sync                                         # зависимости + dev-группа (pytest, mutmut)
uv run python -m first_contact run              # http://127.0.0.1:8080, база data/state.sqlite3
docker compose -f docker-compose.local.yml up -d --build
```

## Инварианты

- Репозиторий публикуется открыто. Внутренние сведения организации —
  только в `CLAUDE.local.md`, он в `.gitignore`.
- `docker-compose.yml` и команды `docker compose …` в `docs/УСТАНОВКА.md` — контракт с клиентами.
- Отправки в Орбиту идемпотентны по `Idempotency-Key`; формат ключей — `docs/РАЗРАБОТЧИКУ.md`, менять
  только вместе с ним.
- Новая колонка БД — строка в `db.MIGRATIONS`, новая настройка — поле `config.Settings` со значением по
  умолчанию.
