# Build stage: compilers and headers exist only here (mysqlclient builds from source).
FROM python:3.11-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    default-libmysqlclient-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt


# Runtime stage: no compiler, runs as an unprivileged user.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOME=/tmp

WORKDIR /app

# libmariadb3: mysqlclient's runtime library. default-mysql-client: mysqldump for
# backups. ffmpeg: video probing/processing. curl: container health checks.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmariadb3 \
    default-mysql-client \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/* && rm -rf /wheels

COPY . /app/

# Writable runtime paths (volumes in compose). The code itself stays root-owned and
# read-only to the service user.
RUN mkdir -p /app/logs /app/staticfiles /app/backups /app/media /tmp/processing \
    && chmod 1777 /app/logs /app/staticfiles /app/backups /app/media /tmp/processing \
    && chmod +x /app/entrypoint.sh

# Compose runs services as ${UID}:${GID}; this is the default for a bare `docker run`.
USER 1000:1000

EXPOSE 8000

CMD ["/app/entrypoint.sh"]
