"""Job management API endpoints."""

import logging
import traceback

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from common.models import Job
from common.permissions import user_can_read_folder, user_is_project_admin

logger = logging.getLogger(__name__)


def _request_domain(request):
    namespace = (
        getattr(request, "resolver_match", None) and request.resolver_match.namespace
    ) or "maxillo"
    return "brain" if namespace == "brain" else "maxillo"


def _job_patient(job):
    return job.brain_patient if job.domain == "brain" else job.patient


def _job_voice_caption(job):
    return job.brain_voice_caption if job.domain == "brain" else job.voice_caption


def _serialize_job(job):
    voice_caption = _job_voice_caption(job)
    patient = _job_patient(job)
    job_data = {
        "id": job.id,
        "domain": job.domain,
        "modality": job.modality_slug,
        "status": job.status,
        "priority": job.priority,
        "input_files": job.input_files,
        "output_files": job.output_files,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "retry_count": job.retry_count,
        "max_retries": job.max_retries,
        "error_logs": job.error_logs,
        "worker_id": job.worker_id,
    }
    if patient:
        job_data["patient_id"] = getattr(patient, "patient_id", None) or getattr(
            patient, "id", None
        )
    if voice_caption:
        job_data["voice_caption_id"] = voice_caption.id
    return job_data


def _user_can_access_job(user, job):
    if user_is_project_admin(user, job.domain):
        return True

    patient = _job_patient(job)
    if not patient:
        voice_caption = _job_voice_caption(job)
        patient = getattr(voice_caption, "patient", None)

    if not patient or getattr(patient, "deleted", False) or not patient.folder:
        return False

    return user_can_read_folder(user, patient.folder, job.domain)


def _apply_job_acl_filter(jobs_qs, user, domain):
    if user_is_project_admin(user, domain):
        return jobs_qs

    if domain == "brain":
        folder_ids = user.brain_folder_access.values_list("folder_id", flat=True)
        return jobs_qs.filter(
            Q(brain_patient__folder_id__in=folder_ids)
            | Q(brain_voice_caption__patient__folder_id__in=folder_ids)
        )

    folder_ids = user.maxillo_folder_access.values_list("folder_id", flat=True)
    return jobs_qs.filter(
        Q(patient__folder_id__in=folder_ids)
        | Q(voice_caption__patient__folder_id__in=folder_ids)
    )


@csrf_exempt
@login_required
@require_http_methods(["GET"])
def get_job_status(request, job_id):
    try:
        domain = _request_domain(request)
        job = Job.objects.select_related(
            "patient", "brain_patient", "voice_caption", "brain_voice_caption"
        ).get(id=job_id, domain=domain)
        if not _user_can_access_job(request.user, job):
            return JsonResponse({"error": "Permission denied"}, status=403)
        return JsonResponse({"success": True, "job": _serialize_job(job)})
    except Job.DoesNotExist:
        logger.error("Job with ID %s not found for status check.", job_id)
        return JsonResponse({"error": "Job not found"}, status=404)
    except Exception as exc:
        logger.error("Error getting job status for %s: %s", job_id, exc)
        logger.error("Full traceback: %s", traceback.format_exc())
        return JsonResponse({"error": str(exc)}, status=500)


@method_decorator(csrf_exempt, name="dispatch")
@method_decorator(login_required, name="dispatch")
class ProcessingJobListView(View):
    """List jobs with optional filtering."""

    def get(self, request):
        try:
            modality = request.GET.get("job_type")
            status = request.GET.get("status")
            limit = int(request.GET.get("limit", 50))
            offset = int(request.GET.get("offset", 0))

            domain = _request_domain(request)
            jobs = Job.objects.filter(domain=domain).select_related(
                "patient", "brain_patient", "voice_caption", "brain_voice_caption"
            )
            jobs = _apply_job_acl_filter(jobs, request.user, domain)

            if modality:
                jobs = jobs.filter(modality_slug=modality)
            if status:
                jobs = jobs.filter(status=status)

            total_count = jobs.count()
            jobs = jobs[offset : offset + limit]
            jobs_data = [_serialize_job(job) for job in jobs]

            return JsonResponse(
                {
                    "success": True,
                    "jobs": jobs_data,
                    "pagination": {
                        "total_count": total_count,
                        "limit": limit,
                        "offset": offset,
                        "has_more": offset + limit < total_count,
                    },
                }
            )
        except Exception as exc:
            logger.error("Error listing processing jobs: %s", exc)
            logger.error("Full traceback: %s", traceback.format_exc())
            return JsonResponse({"error": str(exc)}, status=500)
