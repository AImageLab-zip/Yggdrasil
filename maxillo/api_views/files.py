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
from common.models import FileRegistry, ProjectAccess
from common.permissions import (
    filter_patients_for_user,
    user_can_read_folder,
    user_can_view_caption_content,
    user_is_project_admin,
)
from common.file_access import exists as artifact_exists, streaming_response

logger = logging.getLogger(__name__)


@csrf_exempt
@login_required
@require_http_methods(["GET"])
def serve_file(request, file_id):
    """
    Serve files from FileRegistry by ID with authentication
    URL: /api/processing/files/serve/<file_id>/
    """
    try:
        file_obj = FileRegistry.objects.select_related(
            "patient",
            "brain_patient",
            "voice_caption__patient",
            "brain_voice_caption__patient",
        ).get(id=file_id)
        resolved_file_path = file_obj.file_path
        requested_file_key = (request.GET.get('file_key') or '').strip()

        # CBCT processed files may be stored as a multi-file bundle where the
        # actual NIfTI volume path is in metadata.files.volume_nifti.path.
        if (
            file_obj.file_type == "cbct_processed"
            and file_obj.file_hash == "multi-file"
            and isinstance(file_obj.metadata, dict)
        ):
            files_data = file_obj.metadata.get("files", {})
            volume_nifti = (
                files_data.get("volume_nifti", {})
                if isinstance(files_data, dict)
                else {}
            )
            volume_path = (
                volume_nifti.get("path") if isinstance(volume_nifti, dict) else None
            )
            if volume_path and artifact_exists(volume_path):
                resolved_file_path = volume_path

        request_namespace = (
            getattr(request, "resolver_match", None)
            and request.resolver_match.namespace
        ) or "maxillo"
        file_domain = file_obj.domain or request_namespace
        if file_domain not in ["maxillo", "brain", "laparoscopy"]:
            file_domain = request_namespace

        if not artifact_exists(resolved_file_path):
            raise Http404("File not found")

        # Authentication: Check if user has access to the patient associated with this file
        if file_domain == "brain":
            patient = file_obj.brain_patient
        elif file_domain == "laparoscopy":
            patient = file_obj.laparoscopy_patient
        else:
            patient = file_obj.patient
        if not patient:
            patient = file_obj.patient or file_obj.brain_patient or file_obj.laparoscopy_patient
        if patient:
            if getattr(patient, "deleted", False):
                return JsonResponse({"error": "Patient not found"}, status=404)

            from common.models import Project

            project = Project.objects.filter(slug=file_domain).first()

            can_view = user_is_project_admin(request.user, file_domain) or (
                patient.folder and user_can_read_folder(request.user, patient.folder, file_domain)
            )

            if not can_view:
                logger.warning(
                    f"User {request.user.id} denied access to file {file_id} for patient {patient.patient_id}"
                )
                return JsonResponse({"error": "Permission denied"}, status=403)

            # Check project access if patient belongs to a project
            if project and not user_is_project_admin(request.user, project):
                has_project_access = ProjectAccess.objects.filter(
                    user=request.user, project=project
                ).exists()
                if not has_project_access:
                    logger.warning(
                        f"User {request.user.id} denied project access for file {file_id}"
                    )
                    return JsonResponse({"error": "Project access denied"}, status=403)

            voice_caption = (
                file_obj.brain_voice_caption
                if file_domain == "brain"
                else file_obj.voice_caption
            )
            if not voice_caption:
                voice_caption = file_obj.voice_caption or file_obj.brain_voice_caption
            if voice_caption and not user_can_view_caption_content(
                request.user, voice_caption, file_domain
            ):
                logger.warning(
                    f"User {request.user.id} denied access to voice caption file {file_id}"
                )
                return JsonResponse({"error": "Permission denied"}, status=403)
        else:
            # If file is not associated with a patient, check any project access
            has_any_admin_access = ProjectAccess.objects.filter(
                user=request.user, role="admin"
            ).exists()
            if not has_any_admin_access:
                logger.warning(
                    f"User {request.user.id} denied access to orphaned file {file_id}"
                )
                return JsonResponse({"error": "Permission denied"}, status=403)

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
            (file_obj.metadata or {}).get("original_filename")
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

        from common.models import Project

        namespace = (
            getattr(request, "resolver_match", None)
            and request.resolver_match.namespace
        ) or "maxillo"
        current_project = Project.objects.filter(slug=namespace).first()

        # Build query with authorization filtering
        files = FileRegistry.objects.select_related("patient", "brain_patient")

        files = files.filter(domain=namespace)
        is_admin = user_is_project_admin(request.user, namespace)
        if namespace == "brain":
            files = files.filter(models.Q(brain_patient__isnull=True) | models.Q(brain_patient__deleted=False))
            if not is_admin:
                files = files.filter(brain_patient__isnull=False)
        else:
            files = files.filter(models.Q(patient__isnull=True) | models.Q(patient__deleted=False))
            if not is_admin:
                files = files.filter(patient__isnull=False)

        if not is_admin:
            PatientModel = files.model._meta.apps.get_model('brain' if namespace == 'brain' else 'maxillo', 'Patient')
            allowed_patients = filter_patients_for_user(request.user, PatientModel.objects.all(), namespace).values_list('patient_id', flat=True)
            if namespace == 'brain':
                files = files.filter(brain_patient_id__in=allowed_patients)
            else:
                files = files.filter(patient_id__in=allowed_patients)

        # Apply additional filters
        if file_type:
            files = files.filter(file_type=file_type)
        if patient_id:
            if namespace == "brain":
                files = files.filter(brain_patient__patient_id=patient_id)
            else:
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

            if namespace == "brain" and getattr(file_obj, "brain_patient_id", None):
                file_data["patient_id"] = file_obj.brain_patient_id
            elif getattr(file_obj, "patient_id", None):
                file_data["patient_id"] = file_obj.patient_id
            if namespace == "brain" and getattr(
                file_obj, "brain_voice_caption_id", None
            ):
                file_data["voice_caption_id"] = file_obj.brain_voice_caption_id
            elif file_obj.voice_caption:
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
