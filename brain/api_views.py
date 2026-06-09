"""Brain processing API endpoints."""

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from common.models import FileRegistry, Job
from common.file_access import streaming_response


def health_check(request):
    return JsonResponse({"status": "ok", "domain": "brain"})


class ProcessingJobListView:
    @classmethod
    def as_view(cls):
        def view(request):
            jobs = Job.objects.filter(domain="brain").order_by("-created_at")[:100]
            return JsonResponse({
                "jobs": [
                    {
                        "id": job.id,
                        "modality_slug": job.modality_slug,
                        "status": job.status,
                        "patient_id": job.brain_patient_id,
                    }
                    for job in jobs
                ]
            })

        return view


def get_job_status(request, job_id):
    job = Job.objects.filter(id=job_id, domain="brain").first()
    if not job:
        return JsonResponse({"error": "Job not found"}, status=404)
    return JsonResponse({"id": job.id, "status": job.status, "output_files": job.output_files})


@require_http_methods(["POST"])
def runner_claim_job(request, job_id):
    return get_job_status(request, job_id)


@require_http_methods(["POST"])
def runner_complete_job(request, job_id):
    job = Job.objects.filter(id=job_id, domain="brain").first()
    if not job:
        return JsonResponse({"error": "Job not found"}, status=404)
    job.mark_completed()
    return JsonResponse({"ok": True, "status": job.status})


@require_http_methods(["POST"])
def runner_fail_job(request, job_id):
    job = Job.objects.filter(id=job_id, domain="brain").first()
    if not job:
        return JsonResponse({"error": "Job not found"}, status=404)
    job.status = "failed"
    job.save(update_fields=["status"])
    return JsonResponse({"ok": True, "status": job.status})


def get_file_registry(request):
    files = FileRegistry.objects.filter(domain="brain").order_by("-created_at")[:100]
    return JsonResponse({
        "files": [
            {
                "id": item.id,
                "file_type": item.file_type,
                "file_path": item.file_path,
                "patient_id": item.brain_patient_id,
            }
            for item in files
        ]
    })


def serve_file(request, file_id):
    file_obj = FileRegistry.objects.filter(id=file_id, domain="brain").first()
    if not file_obj:
        return JsonResponse({"error": "File not found"}, status=404)
    return streaming_response(
        path_or_key=file_obj.file_path,
        content_type="application/octet-stream",
        filename=file_obj.file_path.rstrip("/").split("/")[-1] or "file",
    )
