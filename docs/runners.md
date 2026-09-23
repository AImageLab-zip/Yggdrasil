# Distributed runners

CBCT/IOS preprocessing is executed by external Celery runners, not by the web container. The web app only enqueues jobs and exposes a token-protected callback API for runners to report status.

## How job routing works

- The web app enqueues task `RUNNER_TASK_NAME` (default: `yggdrasil.runner.process_job`).
- Jobs are routed to a Celery queue based on `RUNNER_DEFAULT_QUEUE`, optionally overridden per modality (`RUNNER_QUEUE_BY_MODALITY`) or per project (`RUNNER_QUEUE_BY_PROJECT`).

Example modality routing (set in `.env`):

```
RUNNER_DEFAULT_QUEUE=runner_dev
RUNNER_QUEUE_BY_MODALITY={"ios":"runner_ios_dev","bite_classification":"runner_bite_dev","cbct":"runner_cbct_dev"}
RUNNER_QUEUE_BY_PROJECT={}
```

## Runner callback API

Runners authenticate with a bearer token from `RUNNER_API_TOKENS` (comma-separated list of accepted tokens) and identify themselves with the optional `X-Runner-Worker-Id` header.

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/runner/jobs/<id>/claim/` | POST | Runner claims a pending job (409 if already claimed) |
| `/api/runner/jobs/<id>/complete/` | POST | Runner reports success, with `output_files` (object) and `logs` (string) |
| `/api/runner/jobs/<id>/fail/` | POST | Runner reports failure, with `error` message |

All three return 404 if the job doesn't exist, 401 if the token is missing/invalid, and 503 if `RUNNER_API_TOKENS` isn't configured at all.

Example claim request:

```bash
curl -X POST http://localhost:$WEB_EXTERNAL_PORT/api/runner/jobs/123/claim/ \
  -H "Authorization: Bearer $RUNNER_API_TOKENS" \
  -H "X-Runner-Worker-Id: worker-1"
```

## Setting up a worker node

Worker nodes run the Yggdrasil Celery app (`python -m celery -A yggdrasil worker`) against this deployment's Redis broker, with matching queue names / `RUNNER_TASK_NAME`. Redis is published on **loopback only** (`127.0.0.1:${REDIS_EXTERNAL_PORT}`), so a worker must run inside the deployment's Docker network or on the host, or reach it through a tunnel. Do not publish Redis to other hosts: broker access can enqueue to any queue, including the maintenance queue.

The compose `runner-worker` is attached to `app-net-$DOCKER_SUFFIX` and reaches Redis and the web API by service name (`RUNNER_API_BASE_URL`, `CELERY_BROKER_URL` in `.env.worker`). It does **not** load the web app's `.env`: it gets `.env.worker` plus the object-storage settings it presigns job grants with, and runs as `${UID}:${GID}` (see `docker-compose.yml`). The SLURM key and pinned host keys are mounted under `/run/secrets/` and must be readable by that user.

Do not put inline comments after values in `.env.worker`: `python-decouple` treats the comment text as part of the value.

Because Paramiko rejects unknown SSH hosts, create the mounted known-hosts file before starting the worker, for example:

```bash
mkdir -p secrets
ssh-keyscan -p "$SLURM_SSH_PORT" "$SLURM_SSH_HOST" > secrets/slurm_known_hosts
```
