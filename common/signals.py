import logging

from django.conf import settings
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from common.job_routing import is_runner_enabled_for_modality, select_runner_queue
from common.models import Job
from yggdrasil.celery import app as celery_app

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Job)
def _job_pre_save(sender, instance: Job, **kwargs):
    if not instance.pk:
        instance._previous_status = None
        return
    try:
        instance._previous_status = (
            Job.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
        )
    except Exception:
        instance._previous_status = None

    if (
        instance._previous_status != instance.status
        and instance.status in {"pending", "retrying"}
    ):
        instance.output_files = {}
        instance.started_at = None
        instance.completed_at = None
        instance.worker_id = ""
        # Cleared on re-dispatch; the runner worker restamps it (observability only).
        instance.slurm_job_id = ""
        if instance.status == "pending":
            instance.error_logs = ""


def enqueue_runner_task(job) -> bool:
    """Send the runner task for ``job``; return True if it was enqueued.

    The single place a dispatch is performed. ``_job_post_save`` decides only
    *whether* a save warrants one; management commands that re-dispatch an
    existing job call this directly, because a job that is already ``pending``
    never transitions and so never fires the signal.

    This lives here rather than in ``common.job_routing`` because the whole test
    suite patches ``common.signals.celery_app.send_task`` as its dispatch seam —
    including the frozen runner-API contract tests.

    Never raises: a failure is logged and recorded on the job, because nothing
    re-scans the pending table and a stranded job is otherwise indistinguishable
    from a healthy one.
    """
    job_id = getattr(job, "id", None)
    modality_slug = getattr(job, "modality_slug", None)
    try:
        if not is_runner_enabled_for_modality(modality_slug):
            logger.info(
                "Not enqueueing Job %s for disabled modality '%s'",
                job_id,
                modality_slug,
            )
            return False

        queue = select_runner_queue(job)
        task_name = getattr(
            settings, "RUNNER_TASK_NAME", "yggdrasil.runner.process_job"
        )
        celery_app.send_task(task_name, args=[job_id], queue=queue)
        logger.info(
            "Enqueued Job %s to queue '%s' (task=%s)", job_id, queue, task_name
        )
        return True
    except Exception as exc:
        logger.error("Failed to enqueue Job %s: %s", job_id, exc, exc_info=True)
        try:
            Job.objects.filter(pk=getattr(job, "pk", None)).update(
                error_logs=f"enqueue failed: {exc!r}"
            )
        except Exception:
            logger.exception("Could not record enqueue failure on Job %s", job_id)
        return False


@receiver(post_save, sender=Job)
def _job_post_save(sender, instance: Job, created: bool, **kwargs):
    """Dispatch is pure Redis/Celery: enqueue the runner task and nothing more.

    This decides only *whether* a save warrants a dispatch; performing one is
    enqueue_runner_task above, which management commands share.

    The web app knows nothing about how jobs execute — a dedicated Celery worker
    (see common.runner) consumes the queue and drives the cluster.
    """
    if instance.status not in {"pending", "retrying"}:
        return
    prev = getattr(instance, "_previous_status", None)
    if not (created or prev != instance.status):
        return
    enqueue_runner_task(instance)
