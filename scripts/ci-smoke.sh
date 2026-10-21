#!/usr/bin/env bash
# Контракт образа: собирается, запускается не от root, отвечает /healthz и /ready, панель отдаёт страницу.
# Запуск из корня репозитория: ./scripts/ci-smoke.sh
# Нужен только docker. Всё, что создано, удаляется при выходе — и при падении.
set -euo pipefail

name="first-contact-smoke-$$-$(date +%s)"
image="first-contact-smoke:$$-$(date +%s)"

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker image rm -f "$image" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() { echo "SMOKE FAIL: $*" >&2; docker logs "$name" 2>&1 | tail -n 30 >&2 || true; exit 1; }

docker build -q -t "$image" . >/dev/null

docker run -d --name "$name" -e PORT=8090 "$image" >/dev/null

# Изнутри контейнера: порт хоста не занимаем, соседние прогоны не мешают
probe() { docker exec "$name" curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8090$1"; }

for _ in $(seq 1 30); do
  [ "$(probe /healthz || true)" = "200" ] && break
  sleep 1
done

[ "$(probe /healthz)" = "200" ] || fail "/healthz не отвечает 200"
[ "$(probe /ready)" = "200" ] || fail "/ready не отвечает 200"
# Без администратора панель ведёт на /setup — страница первого запуска отдаётся
[ "$(probe /login)" = "303" ] || fail "/login без администратора не перенаправляет на /setup"
[ "$(probe /setup)" = "200" ] || fail "/setup не отдаёт страницу"
# Вебхук Битрикса без токена отвергается, а не падает
[ "$(docker exec "$name" curl -s -o /dev/null -w '%{http_code}' -X POST -d 'event=ONCRMDEALADD' \
    "http://127.0.0.1:8090/bitrix/deal-add")" = "403" ] || fail "/bitrix/deal-add без токена не отвечает 403"

uid="$(docker exec "$name" id -u)"
[ "$uid" != "0" ] || fail "процесс в контейнере работает от root"

echo "SMOKE OK"
