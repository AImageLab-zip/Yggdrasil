#!/bin/bash
set -e

python manage.py collectstatic --noinput

# AUTO_MIGRATE=0 opts out (e.g. extra dev stacks sharing a database with
# another stack that owns migrations).
if [ "${AUTO_MIGRATE:-1}" = "1" ]; then
    python manage.py migrate --noinput
fi

# RUN_DEV_SERVER=1 keeps the Django dev server for local development.
if [ "${RUN_DEV_SERVER:-0}" = "1" ]; then
    exec python manage.py runserver 0.0.0.0:8000
fi

exec gunicorn yggdrasil.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-4}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --access-logfile - \
    --error-logfile -
