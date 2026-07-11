from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings

from common.models import Job


# None overrides neutralize a local .env (treated like absent settings) so
# the expected queue is the built-in default everywhere, incl. CI.
@override_settings(
    RUNNER_DEFAULT_QUEUE=None,
    RUNNER_QUEUE_BY_PROJECT=None,
    RUNNER_QUEUE_BY_MODALITY=None,
)
class JobEnqueueSignalTests(TestCase):
    def setUp(self):
        patcher = mock.patch("common.signals.celery_app.send_task")
        self.mock_send_task = patcher.start()
        self.addCleanup(patcher.stop)

    def _task_name(self):
        return getattr(
            settings, "RUNNER_TASK_NAME", "yggdrasil.runner.process_job"
        )

    def test_pending_job_creation_enqueues_once(self):
        job = Job.objects.create(domain="maxillo", modality_slug="demo")
        self.mock_send_task.assert_called_once_with(
            self._task_name(), args=[job.id], queue="runner"
        )

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"demo": "gpu-q"})
    def test_enqueue_respects_modality_queue_map(self):
        job = Job.objects.create(domain="maxillo", modality_slug="demo")
        self.mock_send_task.assert_called_once_with(
            self._task_name(), args=[job.id], queue="gpu-q"
        )

    def test_processing_job_creation_does_not_enqueue(self):
        Job.objects.create(domain="maxillo", modality_slug="demo", status="processing")
        self.mock_send_task.assert_not_called()

    def test_save_without_status_change_does_not_reenqueue(self):
        job = Job.objects.create(domain="maxillo", modality_slug="demo")
        self.mock_send_task.reset_mock()
        job.priority = 5
        job.save()
        self.mock_send_task.assert_not_called()

    def test_requeue_transition_enqueues_and_resets_worker_fields(self):
        job = Job.objects.create(
            domain="maxillo", modality_slug="demo", status="completed"
        )
        Job.objects.filter(pk=job.pk).update(
            worker_id="worker-a",
            output_files={"result": "objects/out.bin"},
            error_logs="old error",
        )
        job.refresh_from_db()
        self.mock_send_task.assert_not_called()

        job.status = "pending"
        job.save()

        self.mock_send_task.assert_called_once_with(
            self._task_name(), args=[job.id], queue="runner"
        )
        job.refresh_from_db()
        self.assertEqual(job.worker_id, "")
        self.assertEqual(job.output_files, {})
        self.assertEqual(job.error_logs, "")
        self.assertIsNone(job.started_at)
        self.assertIsNone(job.completed_at)

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"other": "q"})
    def test_disabled_modality_is_not_enqueued(self):
        Job.objects.create(domain="maxillo", modality_slug="demo")
        self.mock_send_task.assert_not_called()

    def test_broker_failure_does_not_break_save(self):
        self.mock_send_task.side_effect = RuntimeError("broker down")
        job = Job.objects.create(domain="maxillo", modality_slug="demo")
        self.assertIsNotNone(job.pk)
