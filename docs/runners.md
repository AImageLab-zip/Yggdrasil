# Distributed runners

CBCT/IOS preprocessing is executed by external Celery runners, not by the web container. The web app only enqueues jobs and exposes a token-protected callback API for runners to report status.

## How job routing works

- The web app enqueues task `RUNNER_TASK_NAME` (default: `toothfairy4m_runner.process_job`).
- Jobs are routed to a Celery queue based on `RUNNER_DEFAULT_QUEUE`, optionally overridden per modality (`RUNNER_QUEUE_BY_MODALITY`) or per project (`RUNNER_QUEUE_BY_PROJECT`).

Example modality routing (set in `.env`):

```
RUNNER_DEFAULT_QUEUE=runner_dev
RUNNER_QUEUE_BY_MODALITY={"ios":"runner_ios_dev","bite_classification":"runner_bite_dev","cbct":"runner_cbct_dev","audio":"runner_audio_dev","voice":"runner_audio_dev"}
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

Worker nodes are built from the `toothfairy4m-runner` cookiecutter template (https://github.com/AImageLab-zip/toothfairy4m-runner), pointed at this app's Redis instance (`REDIS_PASSWORD`, `REDIS_EXTERNAL_PORT`) and using matching queue names / `RUNNER_TASK_NAME`.
