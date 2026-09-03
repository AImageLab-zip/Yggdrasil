"""File serving and registry API endpoints."""

from django.http import JsonResponse, Http404, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required
from django.db import models
import contextlib
import os
import re
import logging
import traceback
import mimetypes
from common.models import FileRegistry
from common.permissions import (
    filter_patients_for_user,
    user_is_project_admin,
)
from common.file_access import (
    authorize_file_read,
    exists as artifact_exists,
    streaming_response,
)

logger = logging.getLogger(__name__)


@csrf_exempt
@login_required
@require_http_methods(["GET"])
def serve_file(request, file_id, filename=None, bundle_key=None):
    """
    Serve files from FileRegistry by ID with authentication

    URL: /api/processing/files/serve/<file_id>/
         /api/processing/files/serve/<file_id>/<filename>
         /api/processing/files/serve/<file_id>/key/<bundle_key>/<filename>

    The second, filename-suffixed form exists for Cornerstone3D's NIfTI loader,
    which decides whether to gunzip by testing ``new URL(url).pathname`` for a
    ``.gz`` suffix -- a query parameter cannot carry that (finding F3 of
    docs/cornerstone-roadmap.md).

    ``filename`` is **decorative and deliberately unused**: the bytes served are
    always ``FileRegistry.file_path`` (or the requested bundle member), and the
    Content-Disposition name is still derived from the registry row below. Django's
    ``str`` converter already excludes ``/``, but not reading the segment at all is
    what makes that irrelevant rather than merely survivable.

    The third form carries the bundle key **in the path**, and exists because
    ``?file_key=`` is unusable from the viewer (finding F14).
    ``createNiftiImageIdsAndCacheMetadata.js:174`` builds each slice id as
    ``nifti:${niftiURL}?frame=${i}`` with a literal ``?``, unconditionally, so a URL
    that already has a query string yields ``...?file_key=volume_nifti?frame=0`` --
    two ``?``, so ``frame`` parses as part of the ``file_key`` value and **every
    slice resolves to frame 0**. That is not a hypothetical: a ``cbct_processed`` row
    with ``file_hash == 'multi-file'`` is how the maxillo CBCT display volume is
    stored (``maxillo/views/patient_detail.py:_resolved_cbct_viewer_source``), so
    without a query-free form the volume grid cannot address the volume it exists to
    show.

    ``?file_key=`` is kept, unchanged, for the existing non-Cornerstone callers. When
    both are present they must agree; a mismatch is refused rather than resolved by
    precedence, because the two names would be pointing at different volumes and
    guessing which the caller meant is how the wrong anatomy gets rendered.
    """
    del filename  # see the docstring: URL decoration only, never used to resolve.
    try:
        file_obj = FileRegistry.objects.select_related("patient").get(id=file_id)
        resolved_file_path = file_obj.file_path
        query_file_key = (request.GET.get('file_key') or '').strip()
        path_file_key = (bundle_key or '').strip()
        if path_file_key and query_file_key and path_file_key != query_file_key:
            return JsonResponse(
                {
                    "error": (
                        "Conflicting bundle keys: the path names "
                        f"'{path_file_key}' and file_key names '{query_file_key}'."
                    )
                },
                status=400,
            )
        requested_file_key = path_file_key or query_file_key
        bundle_filename = ""
        bundle_not_found = False

        # CBCT processed files may be stored as a multi-file bundle. Allow a
        # specific metadata.files key, defaulting to the segmentation.
        if (
            file_obj.file_type == "cbct_processed"
            and file_obj.file_hash == "multi-file"
            and isinstance(file_obj.metadata, dict)
        ):
            files_data = file_obj.metadata.get("files", {})
            if isinstance(files_data, dict):
                bundle_key = (
                    requested_file_key
                    if requested_file_key and requested_file_key != "primary"
                    else "segmentation_nifti"
                )
                bundle_file = files_data.get(bundle_key, {})
                bundle_path = (
                    bundle_file.get("path") if isinstance(bundle_file, dict) else None
                )
                if bundle_path and artifact_exists(bundle_path):
                    resolved_file_path = bundle_path
                    bundle_filename = str(bundle_path).split("/")[-1]
                elif requested_file_key and requested_file_key != "primary":
                    bundle_not_found = True

        request_namespace = (
            getattr(request, "resolver_match", None)
            and request.resolver_match.namespace
        ) or "maxillo"

        allowed, error, status = authorize_file_read(
            request.user, file_obj, request_namespace
        )
        if not allowed:
            logger.warning(
                "User %s denied access to file %s (%s)",
                request.user.id,
                file_id,
                error,
            )
            return JsonResponse({"error": error}, status=status)

        if bundle_not_found:
            raise Http404("Requested bundle file not found")

        # Determine content type
        content_type, _ = mimetypes.guess_type(resolved_file_path)
        if not content_type:
            if file_obj.file_type.startswith("cbct"):
                content_type = "application/octet-stream"
            elif file_obj.file_type.startswith("ios"):
                content_type = "model/stl"
            elif file_obj.file_type.startswith("audio"):
                content_type = "audio/webm"
            else:
                content_type = "application/octet-stream"

        filename = (
            bundle_filename
            or (file_obj.metadata or {}).get("original_filename")
            or (file_obj.metadata or {}).get("filename")
            or (
                str(resolved_file_path).split("/")[-1]
                if resolved_file_path
                else f"file_{file_obj.id}"
            )
        )
        safe_filename = filename.replace("\n", " ").replace("\r", " ")

        # Video and audio files need Range-request support so browsers can seek.
        if content_type and (content_type.startswith("video/") or content_type.startswith("audio/")):
            total_size = file_obj.file_size or 0
            range_header = request.META.get("HTTP_RANGE", "").strip()

            if range_header and total_size > 0:
                m = re.match(r"bytes=(\d+)-(\d*)", range_header)
                if m:
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else total_size - 1
                    end = min(end, total_size - 1)
                    length = end - start + 1
                    try:
                        from common.object_storage import get_object_storage as _get_os
                        body, _ = _get_os().get_range(resolved_file_path, f"bytes={start}-{end}")
                        def _iter(b, chunk=512 * 1024):
                            try:
                                while True:
                                    data = b.read(chunk)
                                    if not data:
                                        break
                                    yield data
                            finally:
                                with contextlib.suppress(Exception):
                                    b.close()
                        resp = StreamingHttpResponse(_iter(body), status=206, content_type=content_type)
                        resp["Content-Range"] = f"bytes {start}-{end}/{total_size}"
                        resp["Content-Length"] = str(length)
                        resp["Accept-Ranges"] = "bytes"
                        resp["Content-Disposition"] = f'inline; filename="{safe_filename}"'
                        return resp
                    except Exception as e:
                        logger.warning(f"Range fetch failed for file {file_id}, falling back: {e}")

            # Full response — still advertise Range support and Content-Length
            resp = streaming_response(
                path_or_key=resolved_file_path,
                content_type=content_type,
                filename=safe_filename,
                as_attachment=False,
            )
            resp["Accept-Ranges"] = "bytes"
            if total_size > 0:
                resp["Content-Length"] = str(total_size)
            return resp

        return streaming_response(
            path_or_key=resolved_file_path,
            content_type=content_type,
            filename=filename,
            as_attachment=False,
        )

    except FileRegistry.DoesNotExist:
        logger.error(f"File with ID {file_id} not found in registry.")
        raise Http404("File not found in registry")
    except Http404:
        raise
    except Exception as e:
        logger.error(f"Error serving file {file_id}: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@login_required
@require_http_methods(["GET"])
def get_file_registry(request):
    """
    API endpoint to get file registry information with authentication
    URL: /api/processing/files/
    """
    try:
        # Query parameters
        file_type = request.GET.get("file_type")
        patient_id = request.GET.get("patient_id")
        limit = int(request.GET.get("limit", 50))
        offset = int(request.GET.get("offset", 0))

        # Build query with authorization filtering
        files = FileRegistry.objects.select_related("patient")

        files = files.filter(domain='maxillo')
        is_admin = user_is_project_admin(request.user, 'maxillo')
        files = files.filter(models.Q(patient__isnull=True) | models.Q(patient__deleted=False))
        if not is_admin:
            files = files.filter(patient__isnull=False)

        if not is_admin:
            from maxillo.models import Patient as MaxilloPatient
            allowed_patients = filter_patients_for_user(request.user, MaxilloPatient.objects.all(), 'maxillo').values_list('patient_id', flat=True)
            files = files.filter(patient_id__in=allowed_patients)

        # Apply additional filters
        if file_type:
            files = files.filter(file_type=file_type)
        if patient_id:
            files = files.filter(patient__patient_id=patient_id)

        # Apply pagination
        total_count = files.count()
        files = files[offset : offset + limit]

        files_data = []
        for file_obj in files:
            file_data = {
                "id": file_obj.id,
                "file_type": file_obj.file_type,
                "file_path": file_obj.file_path,
                "file_size": file_obj.file_size,
                "file_hash": file_obj.file_hash,
                "created_at": file_obj.created_at.isoformat(),
                "metadata": file_obj.metadata,
            }

            if getattr(file_obj, "patient_id", None):
                file_data["patient_id"] = file_obj.patient_id
            if file_obj.voice_caption:
                file_data["voice_caption_id"] = file_obj.voice_caption.id
            if file_obj.processing_job:
                file_data["processing_job_id"] = file_obj.processing_job.id

            files_data.append(file_data)

        return JsonResponse(
            {
                "success": True,
                "files": files_data,
                "pagination": {
                    "total_count": total_count,
                    "limit": limit,
                    "offset": offset,
                    "has_more": offset + limit < total_count,
                },
            }
        )

    except Exception as e:
        logger.error(f"Error getting file registry: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return JsonResponse({"error": str(e)}, status=500)
