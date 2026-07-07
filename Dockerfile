FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config \
    default-libmysqlclient-dev \
    build-essential \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    ffmpeg \
    default-mysql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app/

RUN mkdir -p /app/logs /app/staticfiles /app/backups /app/media /tmp/processing \
    && chmod 1777 /app/logs /app/staticfiles /app/backups /app/media /tmp/processing \
    && chmod +x /app/entrypoint.sh

EXPOSE 8000

CMD ["/app/entrypoint.sh"]
