#!/bin/sh
set -eu

mkdir -p /data

# Без токена бота поднимаем только веб-сервер, чтобы контейнер не падал.
if [ -z "${BOT_TOKEN:-}" ]; then
  echo "BOT_TOKEN не задан — запускаю только веб-сервер" >&2
  set -- --web-only "$@"
fi

exec python run.py "$@"
