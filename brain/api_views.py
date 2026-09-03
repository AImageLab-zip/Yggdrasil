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

from common.file_access import authorize_file_read, streaming_response
from common.models import FileRegistry, Job


logger = logging.getLogger(__name__)

_staff_required = user_passes_test(lambda u: u.is_authenticated and u.is_staff)


def health_check(request):
    return JsonResponse({"status": "ok", "domain": "brain"})


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
def serve_file(request, file_id, filename=None, bundle_key=None):
    """Stream a brain FileRegistry entry after checking the requesting user can
    read the owning patient's folder (or is a brain project admin / staff).

    URL: /brain/api/processing/files/serve/<file_id>/
         /brain/api/processing/files/serve/<file_id>/<filename>

    ``filename`` is **decorative and deliberately unused**, exactly as in
    ``maxillo.api_views.files.serve_file``: the bytes served are always
    ``FileRegistry.file_path``. It exists because Cornerstone3D's NIfTI loader
    decides whether to gunzip by testing ``new URL(url).pathname`` for a ``.gz``
    suffix, which a query parameter cannot carry (finding F3).

    It is accepted here because ``brain/app_urls.py`` routes the suffixed form to
    *this* view and not to maxillo's -- the two are separate implementations that
    happen to share a URL shape. Phase 1 registered that route without widening this
    signature, so every suffixed brain URL was a 500 from the moment it was added
    until the Phase 3 harness became its first caller. Nothing had exercised it.

    ``bundle_key`` is accepted and refused. Brain has no multi-file bundles -- only
    ``cbct_processed`` rows carry them -- so a request naming one is asking for
    something that does not exist, and 404 is the honest answer. Ignoring it would
    silently serve the row's own file under a name that promised a different one.
    """
    del filename  # see the docstring: URL decoration only, never used to resolve.
    if bundle_key:
        raise Http404("Brain files are not multi-file bundles")

    file_obj = FileRegistry.objects.filter(id=file_id, domain="brain").first()
    if not file_obj:
        raise Http404("File not found")

    allowed, error, status = authorize_file_read(request.user, file_obj, "brain")
    if not allowed:
        logger.warning(
            "User %s denied access to brain file %s (%s)",
            request.user.id,
            file_id,
            error,
        )
        return JsonResponse({"error": error}, status=status)

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
