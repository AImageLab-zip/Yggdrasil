FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    pkg-config \
    default-libmysqlclient-dev \
    build-essential \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Docker CLI
RUN curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
RUN echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
RUN apt-get update && apt-get install -y docker-ce-cli && rm -rf /var/lib/apt/lists/*

# Add docker group (GID should match host docker group)
RUN groupadd -g 999 docker || true

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create processing directory
RUN mkdir -p /tmp/processing

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"] 
