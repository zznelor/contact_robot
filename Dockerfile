FROM python:3.12-slim

# Утилиты диагностики изнутри контейнера: HTTP, сеть, DNS, порты. psql не ставим — PostgreSQL у робота нет
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    iputils-ping \
    dnsutils \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.7.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 DB_PATH=/data/state.sqlite3 \
    HOST=0.0.0.0 FORWARDED_ALLOW_IPS=* PORT=8080
# HOST=0.0.0.0 — внутри контейнера; снаружи порт открыт только для самого сервера (docker-compose.yml),
# а из интернета панель доступна только через Caddy, поэтому его X-Forwarded-* верим

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
# Права файлов приходят из папки сборщика: при umask 077 код был бы нечитаем для пользователя robot
RUN chmod -R a+rX /app && uv sync --frozen --no-dev

# Не от root; база — в томе /data
RUN useradd --system --uid 10001 robot && mkdir -p /data && chown robot /data
USER robot
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('PORT', '8080') + '/healthz')" || exit 1
CMD ["python", "-m", "first_contact", "run"]
