from django.db import migrations
from django.db.models import Q


RETIRED_MESSAGE = "Legacy audio transcription pipeline retired; use Live Whisper."
ACTIVE_STATUSES = ("pending", "dependency", "processing", "retrying")


def retire_audio_jobs(apps, schema_editor):
    ProcessingStep = apps.get_model("common", "ProcessingStep")
    Job = apps.get_model("common", "Job")
    ProcessingJob = apps.get_model("common", "ProcessingJob")

    ProcessingStep.objects.filter(
        Q(slug__in=("audio", "voice"))
        | Q(modality__slug__in=("audio", "voice"))
    ).update(is_enabled=False)
    Job.objects.filter(
        modality_slug__in=("audio", "voice"), status__in=ACTIVE_STATUSES
    ).update(status="failed", error_logs=RETIRED_MESSAGE, worker_id="")
    ProcessingJob.objects.filter(
        job_type="audio", status__in=ACTIVE_STATUSES
    ).update(status="failed", error_logs=RETIRED_MESSAGE, worker_id="")

    for app_label in ("maxillo", "brain", "laparoscopy"):
        VoiceCaption = apps.get_model(app_label, "VoiceCaption")
        VoiceCaption.objects.filter(
            processing_status__in=("pending", "processing")
        ).update(processing_status="failed")


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0040_processingstep_prefer_processed_for_viewer"),
    ]

    operations = [
        migrations.RunPython(retire_audio_jobs, migrations.RunPython.noop),
    ]
