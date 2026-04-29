"""Job management API endpoints."""

import logging
import traceback

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from common.models import Job

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
        "input_file_path": job.input_file_path,
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


@csrf_exempt
@require_http_methods(["GET"])
def get_job_status(request, job_id):
    try:
        domain = _request_domain(request)
        job = Job.objects.select_related(
            "patient", "brain_patient", "voice_caption", "brain_voice_caption"
        ).get(id=job_id, domain=domain)
        return JsonResponse({"success": True, "job": _serialize_job(job)})
    except Job.DoesNotExist:
        logger.error("Job with ID %s not found for status check.", job_id)
        return JsonResponse({"error": "Job not found"}, status=404)
    except Exception as exc:
        logger.error("Error getting job status for %s: %s", job_id, exc)
        logger.error("Full traceback: %s", traceback.format_exc())
        return JsonResponse({"error": str(exc)}, status=500)


@method_decorator(csrf_exempt, name="dispatch")
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
