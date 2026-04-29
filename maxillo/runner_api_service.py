import logging
from typing import Any, Dict, Optional

from django.db import transaction
from django.utils import timezone

from common.models import Job
from maxillo.file_utils import mark_job_completed, mark_job_failed

logger = logging.getLogger(__name__)


def _project_slug_for_job(job: Job) -> str:
    try:
        if getattr(job, "domain", "maxillo") == "brain":
            return "brain"

        patient = getattr(job, "patient", None)
        project = getattr(patient, "project", None) if patient is not None else None
        slug = getattr(project, "slug", None) if project is not None else None
        if slug:
            return str(slug)
    except Exception:
        pass
    return "maxillo"


def _patient_public_id_for_job(job: Job) -> Optional[int]:
    patient = getattr(job, "brain_patient", None) or getattr(job, "patient", None)
    if patient is None:
        return None
    return getattr(patient, "patient_id", None) or getattr(patient, "id", None)


def _serialize_job_for_runner(job: Job) -> Dict[str, Any]:
    return {
        "id": job.id,
        "domain": getattr(job, "domain", "maxillo"),
        "modality_slug": job.modality_slug,
        "status": job.status,
        "input_file_path": job.input_file_path,
        "output_files": job.output_files or {},
        "project_slug": _project_slug_for_job(job),
        "patient_id": _patient_public_id_for_job(job),
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


def claim_job_for_runner(*, job_id: int, worker_id: str) -> Dict[str, Any]:
    with transaction.atomic():
        job = (
            Job.objects.select_for_update()
            .select_related(
                "patient", "brain_patient", "voice_caption", "brain_voice_caption"
            )
            .get(id=job_id)
        )

        if job.status in {"pending", "retrying"}:
            job.status = "processing"
            if not job.started_at:
                job.started_at = timezone.now()
            job.worker_id = worker_id
            job.save(update_fields=["status", "started_at", "worker_id"])
            return {
                "claimed": True,
                "reason": "claimed",
                "job": _serialize_job_for_runner(job),
            }

        if job.status == "processing" and job.worker_id == worker_id:
            return {
                "claimed": True,
                "reason": "already_claimed_by_same_worker",
                "job": _serialize_job_for_runner(job),
            }

        return {
            "claimed": False,
            "reason": f"job_not_claimable_status_{job.status}",
            "status": job.status,
            "worker_id": job.worker_id,
        }


def complete_job_from_runner(
    *,
    job_id: int,
    worker_id: str,
    output_files: Optional[Dict[str, Any]] = None,
    logs: str = "",
) -> Dict[str, Any]:
    output_files = output_files or {}

    with transaction.atomic():
        job = Job.objects.select_for_update().get(id=job_id)

        if job.status == "completed":
            return {
                "completed": True,
                "reason": "already_completed",
                "status": job.status,
            }

        if job.worker_id and job.worker_id != worker_id:
            return {
                "completed": False,
                "reason": "worker_mismatch",
                "status": job.status,
                "worker_id": job.worker_id,
            }

        if job.status != "processing":
            return {
                "completed": False,
                "reason": f"job_not_in_processing_status_{job.status}",
                "status": job.status,
            }

        success = mark_job_completed(job_id, output_files, logs)
        if not success:
            return {
                "completed": False,
                "reason": "job_not_found",
            }

        job.refresh_from_db(fields=["status"])
        return {
            "completed": True,
            "reason": "completed",
            "status": job.status,
        }


def fail_job_from_runner(
    *, job_id: int, worker_id: str, error_msg: str
) -> Dict[str, Any]:
    with transaction.atomic():
        job = Job.objects.select_for_update().get(id=job_id)

        if job.status in {"completed", "failed"}:
            return {
                "failed": False,
                "reason": f"job_already_{job.status}",
                "status": job.status,
            }

        if job.worker_id and job.worker_id != worker_id:
            return {
                "failed": False,
                "reason": "worker_mismatch",
                "status": job.status,
                "worker_id": job.worker_id,
            }

        success = mark_job_failed(job_id, error_msg, can_retry=True)
        if not success:
            return {
                "failed": False,
                "reason": "job_not_found",
            }

        job.refresh_from_db(fields=["status"])
        return {
            "failed": True,
            "reason": "marked_failed",
            "status": job.status,
        }
