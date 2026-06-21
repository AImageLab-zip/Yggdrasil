# Running the stack

Assumes you've already done the [setup](setup.md) (`.env` created, `DOCKER_SUFFIX` set, networks created).

All commands below assume your shell has `DOCKER_SUFFIX` exported (`export DOCKER_SUFFIX=dev-yourname`, matching your `.env`), since some scripts and container names depend on it.

## Start / stop

```bash
# Start everything in the background (rebuilding the web image if needed)
docker compose --env-file .env up -d --build

# Stop everything (containers removed, volumes kept)
docker compose --env-file .env down

# Restart a single service
docker compose --env-file .env restart web
```

## Logs

```bash
# Follow logs for all services
docker compose --env-file .env logs -f

# Just the web service
docker compose --env-file .env logs -f web
```

## Running Django commands

```bash
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py <command>
```

Common ones:

```bash
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py migrate
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py makemigrations
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py collectstatic --noinput
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py createsuperuser
```

A shell in the web container:

```bash
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX bash
```

## Database access

```bash
docker exec -it toothfairy4m-db-$DOCKER_SUFFIX mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"
```

## Health check

Once the web service is up:

```bash
curl http://localhost:$WEB_EXTERNAL_PORT/api/processing/health/
```

Reports pending/processing job counts and object storage connectivity.

## Resetting

```bash
# Stop and remove containers, keep the MySQL volume
docker compose --env-file .env down

# Stop and wipe the MySQL volume too (irreversible — destroys all DB data)
docker compose --env-file .env down -v
```
