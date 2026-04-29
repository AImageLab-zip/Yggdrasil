import logging

from django.conf import settings
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from common.job_routing import select_runner_queue
from common.models import Job
from toothfairy.celery import app as celery_app

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


@receiver(post_save, sender=Job)
def _job_post_save(sender, instance: Job, created: bool, **kwargs):
    try:
        prev = getattr(instance, "_previous_status", None)
        should_enqueue = False
        if created and instance.status in {"pending", "retrying"}:
            should_enqueue = True
        elif prev != instance.status and instance.status in {"pending", "retrying"}:
            should_enqueue = True

        if not should_enqueue:
            return

        queue = select_runner_queue(instance)
        task_name = getattr(
            settings, "RUNNER_TASK_NAME", "toothfairy4m_runner.process_job"
        )
        celery_app.send_task(task_name, args=[instance.id], queue=queue)
        logger.info(
            "Enqueued Job %s to queue '%s' (task=%s)", instance.id, queue, task_name
        )
    except Exception as exc:
        logger.error(
            "Failed to enqueue Job %s: %s",
            getattr(instance, "id", None),
            exc,
            exc_info=True,
        )
