"""HTTP client for the runner callback API (worker -> web).

Uses the same token-protected endpoints as any runner
(``/api/runner/jobs/<id>/{claim,complete,fail}/``). Going over HTTP rather than the ORM
keeps the worker host-independent — it can later move off-box with no code change.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class ClaimError(RuntimeError):
    """Job could not be claimed (already running/completed) — skip it."""


class ApiError(RuntimeError):
    pass


class JobApiClient:
    def __init__(self, *, base_url=None, token=None, worker_id=None, timeout=60):
        self.base_url = (base_url or getattr(settings, "RUNNER_API_BASE_URL", "")).rstrip("/")
        self.token = token or getattr(settings, "RUNNER_API_TOKEN", "")
        self.worker_id = worker_id or getattr(settings, "RUNNER_WORKER_ID", "slurm-runner")
        self.timeout = timeout
        if not self.base_url:
            raise ApiError("RUNNER_API_BASE_URL is not configured")
        if not self.token:
            raise ApiError("RUNNER_API_TOKEN is not configured")

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Runner-Worker-Id": self.worker_id,
            "Content-Type": "application/json",
        }

    def _post(self, job_id, action, payload):
        url = f"{self.base_url}/runner/jobs/{job_id}/{action}/"
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return resp.status_code, body

    def claim(self, job_id):
        status, body = self._post(job_id, "claim", {})
        if status == 200 and body.get("claimed"):
            return body.get("job", {})
        raise ClaimError(f"job {job_id} not claimable (HTTP {status}): {body.get('reason')}")

    def attach(self, job_id, slurm_job_id):
        """Record the allocation we just submitted, so a later attempt can reattach.

        Best-effort by design: losing the stamp costs resumability, not the run, and
        raising here would fail a job whose sbatch is already queued.
        """
        status, body = self._post(job_id, "attach", {"slurm_job_id": str(slurm_job_id)})
        if status != 200:
            logger.error("attach job %s -> HTTP %s: %s", job_id, status, body)
        return body

    def complete(self, job_id, output_files, logs=""):
        status, body = self._post(
            job_id, "complete", {"output_files": output_files, "logs": logs}
        )
        if status != 200:
            raise ApiError(f"complete job {job_id} -> HTTP {status}: {body}")
        return body

    def fail(self, job_id, error):
        status, body = self._post(job_id, "fail", {"error": error})
        if status != 200:
            # Log but don't raise; the job already failed and we don't want to mask it.
            logger.error("fail job %s -> HTTP %s: %s", job_id, status, body)
        return body
