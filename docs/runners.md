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

Worker nodes run the Yggdrasil Celery app (`python -m celery -A yggdrasil worker`) pointed at this app's public/host-published Redis endpoint (`REDIS_PASSWORD`, `REDIS_EXTERNAL_PORT`) and using matching queue names / `RUNNER_TASK_NAME`.

The compose `runner-worker` is intentionally not attached to `app-net-$DOCKER_SUFFIX`; it uses Docker's default bridge network and must reach both Redis and the web API through externally routable URLs from `.env.worker`. Set `RUNNER_API_BASE_URL` to the public HTTPS API URL in production. uvicorn does not force HTTPS by itself; redirects come from Django settings or the reverse proxy.

Do not put inline comments after values in `.env.worker`: `python-decouple` treats the comment text as part of the value.

Because Paramiko rejects unknown SSH hosts, create the mounted known-hosts file before starting the worker, for example:

```bash
mkdir -p secrets
ssh-keyscan -p "$SLURM_SSH_PORT" "$SLURM_SSH_HOST" > secrets/slurm_known_hosts
```
