"""Brain processing API endpoints.

Runner callbacks (claim/complete/fail) are intentionally NOT defined here.
External runners are domain-agnostic — they use the single token-authenticated
contract at ``/api/runner/jobs/<id>/...`` (``maxillo/api_views/runner.py``),
which operates on a global ``Job`` id regardless of domain. Per-domain runner
endpoints used to be duplicated here without any auth; they were removed.

The remaining endpoints require authentication and enforce the same folder ACL
the rest of the brain app uses. ``serve_file`` mirrors ``maxillo`` file serving
but for the brain domain (patients relate to folders via the ``folders`` M2M).
"""

import logging
import mimetypes

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_http_methods

from common.file_access import streaming_response
from common.models import FileRegistry, Job
from common.permissions import (
    user_can_read_folder,
    user_can_view_caption_content,
    user_is_project_admin,
)

logger = logging.getLogger(__name__)

_staff_required = user_passes_test(lambda u: u.is_authenticated and u.is_staff)


def health_check(request):
    return JsonResponse({"status": "ok", "domain": "brain"})


def _user_can_read_brain_patient(user, patient):
    if patient is None or getattr(patient, "deleted", False):
        return False
    if user_is_project_admin(user, "brain"):
        return True
    return any(
        user_can_read_folder(user, f, "brain") for f in patient.folders.all()
    )


@login_required
@_staff_required
def _job_list(request):
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


class ProcessingJobListView:
    @classmethod
    def as_view(cls):
        return _job_list


@login_required
@_staff_required
def get_job_status(request, job_id):
    job = Job.objects.filter(id=job_id, domain="brain").first()
    if not job:
        return JsonResponse({"error": "Job not found"}, status=404)
    return JsonResponse({"id": job.id, "status": job.status, "output_files": job.output_files})


@login_required
@_staff_required
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


@login_required
@require_http_methods(["GET"])
def serve_file(request, file_id):
    """Stream a brain FileRegistry entry after checking the requesting user can
    read the owning patient's folder (or is a brain project admin / staff)."""
    file_obj = FileRegistry.objects.filter(id=file_id, domain="brain").first()
    if not file_obj:
        raise Http404("File not found")

    patient = file_obj.brain_patient
    voice_caption = file_obj.brain_voice_caption
    if voice_caption is not None:
        if not user_can_view_caption_content(request.user, voice_caption, "brain"):
            return JsonResponse({"error": "Permission denied"}, status=403)
    else:
        if not _user_can_read_brain_patient(request.user, patient):
            logger.warning(
                "User %s denied access to brain file %s", request.user.id, file_id
            )
            return JsonResponse({"error": "Permission denied"}, status=403)

    path = file_obj.file_path
    if not path:
        raise Http404("File not found")
    content_type, _ = mimetypes.guess_type(path)
    content_type = content_type or "application/octet-stream"
    resp = streaming_response(
        path_or_key=path,
        content_type=content_type,
        filename=path.rstrip("/").split("/")[-1] or "file",
    )
    if content_type.startswith(("video/", "audio/")):
        resp["Accept-Ranges"] = "bytes"
        if file_obj.file_size:
            resp["Content-Length"] = str(file_obj.file_size)
    return resp
