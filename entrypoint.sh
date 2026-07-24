#!/bin/bash
set -e

python manage.py collectstatic --noinput

# AUTO_MIGRATE=0 opts out (e.g. extra dev stacks sharing a database with
# another stack that owns migrations).
if [ "${AUTO_MIGRATE:-1}" = "1" ]; then
    python manage.py migrate --noinput
fi

# RUN_DEV_SERVER=1 keeps auto-reload for local development.
if [ "${RUN_DEV_SERVER:-0}" = "1" ]; then
    exec uvicorn yggdrasil.asgi:application --host 0.0.0.0 --port 8000 --reload
fi

exec uvicorn yggdrasil.asgi:application \
    --host 0.0.0.0 \
    --port 8000 \
    --workers "${ASGI_WORKERS:-4}" \
    --timeout-keep-alive "${ASGI_KEEP_ALIVE:-5}" \
    --access-log
