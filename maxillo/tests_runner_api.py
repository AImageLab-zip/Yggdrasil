"""Contract tests for the external runner HTTP API.

External runners (claim/complete/fail + bearer-token auth) depend on this
exact behavior. These tests freeze the contract: any change that makes them
fail is a breaking change for deployed runners and needs explicit
maintainer sign-off. See docs/modernization-roadmap.md (risk register).
"""

import json
from unittest import mock

from django.test import TestCase, override_settings

from common.models import Job

TOKEN = "test-token"

CLAIM_JOB_PAYLOAD_KEYS = {
    "id",
    "domain",
    "modality_slug",
    "status",
    "input_files",
    "output_files",
    "project_slug",
    "patient_id",
    "created_at",
    # Added for the SLURM-over-SSH runner worker (Yggdrasil 2.0): tells the worker
    # which ALGO_BASE_DIR/<algo_name>/run.sbatch to submit for this job's step.
    "algo_name",
}


@override_settings(RUNNER_API_TOKENS={TOKEN})
class RunnerApiTestCase(TestCase):
    def setUp(self):
        patcher = mock.patch("common.signals.celery_app.send_task")
        self.mock_send_task = patcher.start()
        self.addCleanup(patcher.stop)

    def _job(self, status="pending", **kwargs):
        return Job.objects.create(
            domain="maxillo", modality_slug="demo", status=status, **kwargs
        )

    def _post(self, path, token=TOKEN, worker=None, body=None, raw_body=None):
        headers = {}
        if token is not None:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        if worker is not None:
            headers["HTTP_X_RUNNER_WORKER_ID"] = worker
        if raw_body is not None:
            data = raw_body
        elif body is not None:
            data = json.dumps(body)
        else:
            data = ""
        return self.client.post(
            path, data=data, content_type="application/json", **headers
        )

    def _endpoints(self, job_id):
        return [
            f"/api/runner/jobs/{job_id}/claim/",
            f"/api/runner/jobs/{job_id}/complete/",
            f"/api/runner/jobs/{job_id}/fail/",
        ]


class RunnerAuthContractTests(RunnerApiTestCase):
    def test_missing_token_is_401(self):
        job = self._job()
        for path in self._endpoints(job.id):
            with self.subTest(path=path):
                self.assertEqual(self._post(path, token=None).status_code, 401)

    def test_wrong_token_is_401(self):
        job = self._job()
        for path in self._endpoints(job.id):
            with self.subTest(path=path):
                self.assertEqual(self._post(path, token="nope").status_code, 401)

    def test_malformed_authorization_header_is_401(self):
        job = self._job()
        path = self._endpoints(job.id)[0]
        for header in (TOKEN, f"Token {TOKEN}", "Bearer"):
            with self.subTest(header=header):
                response = self.client.post(
                    path, content_type="application/json", HTTP_AUTHORIZATION=header
                )
                self.assertEqual(response.status_code, 401)

    @override_settings(RUNNER_API_TOKENS=set())
    def test_unconfigured_tokens_is_503(self):
        job = self._job()
        for path in self._endpoints(job.id):
            with self.subTest(path=path):
                response = self._post(path)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(
                    response.json(),
                    {"error": "Runner API tokens are not configured"},
                )

    def test_unknown_job_is_404(self):
        for path in self._endpoints(999999):
            with self.subTest(path=path):
                response = self._post(path)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {"error": "Job not found"})

    def test_get_is_405(self):
        job = self._job()
        for path in self._endpoints(job.id):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 405)

    def test_maxillo_mount_answers_identically(self):
        # Runners may be configured against either mount; both must work.
        job = self._job()
        response = self._post(f"/maxillo/api/runner/jobs/{job.id}/claim/", worker="w1")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["claimed"])


class RunnerClaimContractTests(RunnerApiTestCase):
    def test_claim_pending_job(self):
        job = self._job(input_files={"scan": "objects/scan.nii.gz"})
        response = self._post(f"/api/runner/jobs/{job.id}/claim/", worker="worker-a")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["claimed"])
        self.assertEqual(data["reason"], "claimed")
        # Frozen payload shape: removing or renaming keys breaks runners.
        self.assertEqual(set(data["job"].keys()), CLAIM_JOB_PAYLOAD_KEYS)
        self.assertEqual(data["job"]["id"], job.id)
        self.assertEqual(data["job"]["domain"], "maxillo")
        self.assertEqual(data["job"]["modality_slug"], "demo")
        self.assertEqual(data["job"]["status"], "processing")
        self.assertEqual(data["job"]["input_files"], {"scan": "objects/scan.nii.gz"})
        self.assertEqual(data["job"]["project_slug"], "maxillo")
        self.assertIsNone(data["job"]["patient_id"])
        job.refresh_from_db()
        self.assertEqual(job.status, "processing")
        self.assertEqual(job.worker_id, "worker-a")
        self.assertIsNotNone(job.started_at)

    def test_claim_retrying_job(self):
        job = self._job(status="retrying")
        response = self._post(f"/api/runner/jobs/{job.id}/claim/", worker="worker-a")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["claimed"])

    def test_claim_without_worker_header_defaults_worker_id(self):
        job = self._job()
        response = self._post(f"/api/runner/jobs/{job.id}/claim/")
        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.worker_id, "external-runner")

    def test_reclaim_by_same_worker_is_idempotent(self):
        job = self._job(status="processing", worker_id="worker-a")
        response = self._post(f"/api/runner/jobs/{job.id}/claim/", worker="worker-a")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["claimed"])
        self.assertEqual(data["reason"], "already_claimed_by_same_worker")

    def test_claim_processing_job_of_other_worker_is_409(self):
        job = self._job(status="processing", worker_id="worker-a")
        response = self._post(f"/api/runner/jobs/{job.id}/claim/", worker="worker-b")
        self.assertEqual(response.status_code, 409)
        data = response.json()
        self.assertFalse(data["claimed"])
        self.assertEqual(data["reason"], "job_not_claimable_status_processing")
        self.assertEqual(data["status"], "processing")
        self.assertEqual(data["worker_id"], "worker-a")

    def test_claim_completed_job_is_409(self):
        job = self._job(status="completed")
        response = self._post(f"/api/runner/jobs/{job.id}/claim/", worker="worker-a")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["reason"], "job_not_claimable_status_completed"
        )


class RunnerCompleteContractTests(RunnerApiTestCase):
    def _complete(self, job, worker="worker-a", **kwargs):
        return self._post(
            f"/api/runner/jobs/{job.id}/complete/", worker=worker, **kwargs
        )

    def test_complete_processing_job(self):
        job = self._job(status="processing", worker_id="worker-a")
        # Keep object storage out of the loop; the HTTP contract is what
        # matters here, not FileRegistry bookkeeping.
        with mock.patch("maxillo.file_utils.artifact_exists", return_value=False):
            response = self._complete(
                job,
                body={"output_files": {"result": "objects/out.bin"}, "logs": "done"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["completed"])
        self.assertEqual(data["reason"], "completed")
        self.assertEqual(data["status"], "completed")
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.output_files, {"result": "objects/out.bin"})
        self.assertIsNotNone(job.completed_at)

    def test_complete_with_empty_body_succeeds(self):
        job = self._job(status="processing", worker_id="worker-a")
        response = self._complete(job)
        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.output_files, {})

    def test_complete_already_completed_is_idempotent_200(self):
        job = self._job(status="completed")
        response = self._complete(job, body={})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["completed"])
        self.assertEqual(data["reason"], "already_completed")

    def test_complete_worker_mismatch_is_409(self):
        job = self._job(status="processing", worker_id="worker-a")
        response = self._complete(job, worker="worker-b", body={})
        self.assertEqual(response.status_code, 409)
        data = response.json()
        self.assertFalse(data["completed"])
        self.assertEqual(data["reason"], "worker_mismatch")
        self.assertEqual(data["worker_id"], "worker-a")

    def test_complete_pending_job_is_409(self):
        job = self._job(status="pending")
        response = self._complete(job, body={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["reason"], "job_not_in_processing_status_pending"
        )

    def test_complete_invalid_json_is_400(self):
        job = self._job(status="processing", worker_id="worker-a")
        response = self._complete(job, raw_body="{not json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "Invalid JSON body"})

    def test_complete_output_files_must_be_object(self):
        job = self._job(status="processing", worker_id="worker-a")
        response = self._complete(job, body={"output_files": ["a"]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "output_files must be an object"})

    def test_complete_logs_must_be_string(self):
        job = self._job(status="processing", worker_id="worker-a")
        response = self._complete(job, body={"logs": 5})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "logs must be a string"})


class RunnerFailContractTests(RunnerApiTestCase):
    def _fail(self, job, worker="worker-a", **kwargs):
        return self._post(f"/api/runner/jobs/{job.id}/fail/", worker=worker, **kwargs)

    def test_fail_processing_job(self):
        job = self._job(status="processing", worker_id="worker-a")
        response = self._fail(job, body={"error": "boom"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["failed"])
        self.assertEqual(data["reason"], "marked_failed")
        job.refresh_from_db()
        self.assertEqual(job.error_logs, "boom")
        self.assertEqual(job.status, "retrying")
        self.assertEqual(job.retry_count, 1)
        self.mock_send_task.assert_called_once()

    def test_fail_error_msg_alias_accepted(self):
        job = self._job(status="processing", worker_id="worker-a")
        response = self._fail(job, body={"error_msg": "kaboom"})
        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.error_logs, "kaboom")

    def test_fail_without_error_uses_default_message(self):
        job = self._job(status="processing", worker_id="worker-a")
        response = self._fail(job)
        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.error_logs, "Runner error")

    def test_fail_non_string_error_is_400(self):
        job = self._job(status="processing", worker_id="worker-a")
        response = self._fail(job, body={"error": {"code": 1}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "error must be a string"})

    def test_fail_worker_mismatch_is_409(self):
        job = self._job(status="processing", worker_id="worker-a")
        response = self._fail(job, worker="worker-b", body={"error": "boom"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["reason"], "worker_mismatch")

    def test_fail_completed_job_is_409(self):
        job = self._job(status="completed")
        response = self._fail(job, body={"error": "boom"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["reason"], "job_already_completed")

    def test_fail_failed_job_is_409(self):
        job = self._job(status="failed")
        response = self._fail(job, body={"error": "boom"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["reason"], "job_already_failed")
