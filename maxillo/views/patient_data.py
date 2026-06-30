"""Patient data API endpoints for serving scan data."""

from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils import timezone
import os
import logging
import tempfile
import hashlib
import json
from PIL import Image

from common.file_access import exists as artifact_exists, streaming_response
from common.permissions import (
    user_can_read_folder,
    user_can_write_annotations,
    user_is_project_admin,
)
from common.object_storage import get_object_storage
from common.models import FileRegistry, Modality
from .domain import get_domain_models

logger = logging.getLogger(__name__)


def _serve_file_url(request, file_id):
    namespace = (
        getattr(request, "resolver_match", None) and request.resolver_match.namespace
    ) or "maxillo"
    return reverse(f"{namespace}:api_serve_file", kwargs={"file_id": file_id})


def _can_read_patient(request, patient):
    if user_is_project_admin(request.user, request):
        return True
    if not patient.folder:
        return False
    return user_can_read_folder(request.user, patient.folder, request)


def _can_write_patient(request, patient):
    if user_is_project_admin(request.user, request):
        return True
    return bool(
        patient.folder and user_can_write_annotations(request.user, patient.folder, request)
    )


def _content_type_for_image_path(file_path):
    ext = os.path.splitext((file_path or "").lower())[1]
    if ext in [".jpg", ".jpeg"]:
        return "image/jpeg"
    if ext == ".gif":
        return "image/gif"
    if ext == ".webp":
        return "image/webp"
    return "image/png"


def _latest_official_image_file(patient, file_types, *, source_file_id=None, image_index=None):
    qs = patient.files.filter(file_type__in=file_types)
    if source_file_id is not None:
        qs = qs.filter(metadata__source_file_id=source_file_id)
    elif image_index is not None:
        qs = qs.filter(metadata__image_index=image_index)
    return qs.order_by("-created_at", "-id").first()


@login_required
def patient_viewer_data(request, patient_id):
    """API endpoint to provide scan data for 3D viewer"""
    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_read_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)

    # Determine modality status using Jobs (use 'ios' modality slug from request context or default)
    modality_slug = "ios"  # This endpoint specifically serves IOS data
    try:
        from common.models import Job as _Job

        job_filter = {
            "domain": "maxillo",
            "modality_slug": modality_slug,
            "status": "processing",
            "patient_id": patient.patient_id,
        }
        if _Job.objects.filter(**job_filter).exists():
            return JsonResponse(
                {
                    "error": f"{modality_slug.upper()} scans are still being processed",
                    "status": "processing",
                    "message": "The scans are being processed. This may take a few minutes.",
                },
                status=202,
            )
        failed_filter = {
            "domain": "maxillo",
            "modality_slug": modality_slug,
            "status": "failed",
            "patient_id": patient.patient_id,
        }
        if _Job.objects.filter(**failed_filter).exists():
            return JsonResponse(
                {
                    "error": f"{modality_slug.upper()} processing failed",
                    "status": "failed",
                    "message": "The scan processing failed. Please try uploading again or contact support.",
                },
                status=500,
            )
    except Exception:
        pass

    # Try to get scan URLs from FileRegistry
    upper_scan_url = None
    lower_scan_url = None

    # Check FileRegistry for processed files first, then raw files
    try:
        # Look for processed files first
        processed_files = patient.get_ios_processed_files()
        if processed_files["upper"] and processed_files["lower"]:
            upper_scan_url = _serve_file_url(request, processed_files["upper"].id)
            lower_scan_url = _serve_file_url(request, processed_files["lower"].id)
        else:
            # Fallback to raw files from FileRegistry
            raw_files = patient.get_ios_raw_files()
            if raw_files["upper"] and raw_files["lower"]:
                upper_scan_url = _serve_file_url(request, raw_files["upper"].id)
                lower_scan_url = _serve_file_url(request, raw_files["lower"].id)
    except Exception:
        pass

    if not upper_scan_url or not lower_scan_url:
        return JsonResponse(
            {"error": "No IOS scan data available", "status": "not_found"}, status=404
        )

    # Ensure URLs use HTTPS if the request came over HTTPS
    def build_secure_uri(request, url):
        # Check if request is secure (either direct HTTPS or behind proxy)
        is_secure = (
            request.is_secure() or request.META.get("HTTP_X_FORWARDED_PROTO") == "https"
        )

        # Always use HTTPS if the request is secure, regardless of the original URL
        if is_secure:
            if url.startswith("/"):
                # Relative URL - build absolute URL with HTTPS
                return f"https://{request.get_host()}{url}"
            elif url.startswith("http://"):
                # HTTP URL - convert to HTTPS
                return url.replace("http://", "https://", 1)
            elif url.startswith("https://"):
                # Already HTTPS - return as-is
                return url
            else:
                # Any other case - assume it's a relative URL and make it HTTPS
                return f"https://{request.get_host()}/{url.lstrip('/')}"
        else:
            # For non-secure requests, use standard build_absolute_uri
            return request.build_absolute_uri(url)

    is_secure = (
        request.is_secure() or request.META.get("HTTP_X_FORWARDED_PROTO") == "https"
    )
    logger.debug(
        f"Request secure: {request.is_secure()}, X-Forwarded-Proto: {request.META.get('HTTP_X_FORWARDED_PROTO')}, is_secure: {is_secure}"
    )
    logger.debug(f"Original URLs - upper: {upper_scan_url}, lower: {lower_scan_url}")

    upper_url = build_secure_uri(request, upper_scan_url)
    lower_url = build_secure_uri(request, lower_scan_url)

    logger.debug(f"Final URLs - upper: {upper_url}, lower: {lower_url}")

    data = {
        "upper_scan_url": upper_url,
        "lower_scan_url": lower_url,
        "patient_info": {
            "patient_id": patient.patient_id,
        },
    }

    return JsonResponse(data)


@login_required
def patient_cbct_data(request, patient_id):
    """API endpoint to serve CBCT data"""
    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_read_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)

    # Determine modality status using Jobs (use 'cbct' modality slug for this endpoint)
    modality_slug = "cbct"  # This endpoint specifically serves CBCT data
    try:
        from common.models import Job as _Job

        job_filter = {
            "domain": "maxillo",
            "modality_slug": modality_slug,
            "status": "processing",
            "patient_id": patient.patient_id,
        }
        if _Job.objects.filter(**job_filter).exists():
            return JsonResponse(
                {
                    "error": f"{modality_slug.upper()} is still being processed",
                    "status": "processing",
                    "message": "The volume is being converted to NIfTI format. This may take a few minutes.",
                },
                status=202,
            )
        failed_filter = {
            "domain": "maxillo",
            "modality_slug": modality_slug,
            "status": "failed",
            "patient_id": patient.patient_id,
        }
        if _Job.objects.filter(**failed_filter).exists():
            return JsonResponse(
                {
                    "error": f"{modality_slug.upper()} processing failed",
                    "status": "failed",
                    "message": "The volume processing failed. Please try uploading again or contact support.",
                },
                status=500,
            )
    except Exception:
        pass

    # Get CBCT file path from raw NIfTI uploads.
    file_path = None

    # Use raw CBCT if available.
    if not file_path:
        try:
            # Do not rely on get_cbct_raw_file() because legacy data may contain
            # multiple cbct_raw rows (including non-NIfTI files).
            raw_entries = patient.files.filter(file_type="cbct_raw").order_by(
                "-created_at"
            )
            for raw_entry in raw_entries:
                raw_path = raw_entry.file_path
                if not raw_path:
                    continue
                if (
                    raw_path.endswith(".nii") or raw_path.endswith(".nii.gz")
                ) and artifact_exists(raw_path):
                    file_path = raw_path
                    break
        except Exception:
            pass

    if not file_path or not artifact_exists(file_path):
        return JsonResponse(
            {"error": "No CBCT data available", "status": "not_found"}, status=404
        )

    try:
        return streaming_response(
            path_or_key=file_path,
            content_type="application/octet-stream",
            filename=f"cbct_{patient_id}.nii.gz",
            as_attachment=True,
        )

    except Exception as e:
        logger.error(f"Error serving CBCT data: {e}", exc_info=True)
        return JsonResponse(
            {"error": f"Failed to load CBCT data: {str(e)}"}, status=500
        )


@login_required
def patient_volume_data(request, patient_id, modality_slug):
    """Generic API endpoint to serve NIfTI volume for arbitrary modality (no panoramic).

    Strategy:
    - Use latest FileRegistry entry for (patient, modality) that endswith .nii or .nii.gz
    """
    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_read_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)
    try:
        from common.models import FileRegistry as _FR
    except Exception:
        return JsonResponse({"error": "File registry unavailable"}, status=500)
    file_path = None
    # Use the latest raw NIfTI.
    if not file_path:
        try:
            raw_filter = {"domain": domain, "modality__slug": modality_slug}
            if modality_slug == "cbct":
                raw_filter["file_type"] = "cbct_raw"
            if domain == "brain":
                raw_filter["brain_patient_id"] = patient.patient_id
            else:
                raw_filter["patient_id"] = patient.patient_id
    try:
        processed_filter = {
            "domain": "maxillo",
            "modality__slug": modality_slug,
            "file_type": "cbct_processed",
            "patient_id": patient.patient_id,
        }
        processed = _FR.objects.filter(**processed_filter).first()
        if (
            processed
            and processed.file_hash == "multi-file"
            and "files" in processed.metadata
        ):
            files_data = processed.metadata.get("files", {})
            nifti = files_data.get("volume_nifti", {})
            vol_path = nifti.get("path")
            if vol_path and artifact_exists(vol_path):
                file_path = vol_path
    except Exception:
        pass
    # Fallback: use the latest raw NIfTI
    if not file_path:
        try:
            raw_filter = {
                "domain": "maxillo",
                "modality__slug": modality_slug,
                "patient_id": patient.patient_id,
            }
            raw = _FR.objects.filter(**raw_filter).order_by("-created_at").first()
            if (
                raw
                and raw.file_path
                and (
                    raw.file_path.endswith(".nii") or raw.file_path.endswith(".nii.gz")
                )
                and artifact_exists(raw.file_path)
            ):
                file_path = raw.file_path
        except Exception:
            pass
    if not file_path:
        return JsonResponse(
            {"error": f"No volume data for {modality_slug}"}, status=404
        )
    try:
        return streaming_response(
            path_or_key=file_path,
            content_type="application/octet-stream",
            filename=f"{modality_slug}_{patient_id}.nii.gz",
            as_attachment=True,
        )
    except Exception as e:
        return JsonResponse({"error": f"Failed to load volume: {e}"}, status=500)


@login_required
def patient_panoramic_data(request, patient_id):
    """API endpoint to serve panoramic image data

    Only explicit panoramic modality uploads are served here. CBCT processing
    no longer generates or exposes a panoramic preview.
    """

    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_read_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)

    # PRIORITY 1: Check for uploaded panoramic modality file
    try:
        from common.models import FileRegistry

        # Prefer manually processed panoramic, then raw upload.
        panoramic_file = (
            patient.files.filter(file_type="panoramic_processed")
            .order_by("-created_at", "-id")
            .first()
        )
        if not panoramic_file:
            panoramic_file = (
                patient.files.filter(file_type="panoramic_raw")
                .order_by("-created_at", "-id")
                .first()
            )
        if not panoramic_file:
            panoramic_file = (
                patient.files.filter(modality__slug="panoramic")
                .order_by("-created_at", "-id")
                .first()
            )

        if panoramic_file and artifact_exists(panoramic_file.file_path):
            source_file_id = (
                (panoramic_file.metadata or {}).get("source_file_id")
                if isinstance(panoramic_file.metadata, dict)
                else None
            )
            source_file_id = source_file_id or panoramic_file.id
            if request.GET.get("meta") == "1":
                return JsonResponse(
                    {
                        "url": _serve_file_url(request, panoramic_file.id),
                        "source_file_id": source_file_id,
                        "raw_url": _serve_file_url(request, source_file_id),
                        "is_processed": panoramic_file.file_type.endswith("_processed"),
                    }
                )
            logger.debug(f"Serving uploaded panoramic file: {panoramic_file.file_path}")
            # Determine content type based on file extension
            file_ext = os.path.splitext(panoramic_file.file_path)[1].lower()
            content_type = "image/png"
            if file_ext in [".jpg", ".jpeg"]:
                content_type = "image/jpeg"
            elif file_ext == ".gif":
                content_type = "image/gif"
            elif file_ext == ".webp":
                content_type = "image/webp"

            return streaming_response(
                path_or_key=panoramic_file.file_path,
                content_type=content_type,
                filename=f"panoramic_{patient_id}{file_ext}",
                as_attachment=False,
            )
    except Exception as e:
        logger.warning(f"Error checking for uploaded panoramic file: {e}")

    return JsonResponse(
        {"error": "No panoramic modality file available", "status": "not_found"},
        status=404,
    )


@login_required
def patient_intraoral_data(request, patient_id):
    """API endpoint to serve intraoral photographs data"""

    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_read_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)

    # Get intraoral images from FileRegistry
    try:
        raw_files = patient.files.filter(file_type="intraoral_raw").order_by(
            "metadata__image_index", "created_at", "id"
        )

        if not raw_files.exists():
            legacy_files = patient.files.filter(
                file_type__in=["intraoral-photo_processed", "intraoral_processed"]
            ).order_by(
                "metadata__image_index", "created_at", "id"
            )
            if not legacy_files.exists():
                return JsonResponse({"error": "No intraoral photographs found"}, status=404)
            images_data = []
            for fallback_index, file_obj in enumerate(legacy_files, start=1):
                if not artifact_exists(file_obj.file_path):
                    continue
                image_index = (
                    file_obj.metadata.get("image_index", fallback_index)
                    if isinstance(file_obj.metadata, dict)
                    else fallback_index
                )
                images_data.append(
                    {
                        "id": file_obj.id,
                        "source_file_id": file_obj.id,
                        "index": image_index,
                        "original_filename": (
                            file_obj.metadata.get("original_filename", "")
                            if isinstance(file_obj.metadata, dict)
                            else ""
                        ),
                        "is_processed": True,
                        "edit_meta": (
                            file_obj.metadata.get("edit_meta")
                            if isinstance(file_obj.metadata, dict)
                            else None
                        ),
                        "url": _serve_file_url(request, file_obj.id),
                    }
                )
            if not images_data:
                return JsonResponse(
                    {"error": "No intraoral image files found in storage"},
                    status=404,
                )
            return JsonResponse({"images": images_data, "count": len(images_data)})

        images_data = []
        for fallback_index, file_obj in enumerate(raw_files, start=1):
            if artifact_exists(file_obj.file_path):
                image_index = 0
                if isinstance(file_obj.metadata, dict):
                    image_index = file_obj.metadata.get("image_index", 0) or fallback_index
                processed_file = _latest_official_image_file(
                    patient,
                    ["intraoral-photo_processed", "intraoral_processed"],
                    source_file_id=file_obj.id,
                )
                if not processed_file:
                    processed_file = _latest_official_image_file(
                        patient,
                        ["intraoral-photo_processed", "intraoral_processed"],
                        image_index=image_index,
                    )
                official_file = processed_file or file_obj
                images_data.append(
                    {
                        "id": official_file.id,
                        "source_file_id": file_obj.id,
                        "index": image_index,
                        "original_filename": (
                            file_obj.metadata.get("original_filename", "")
                            if isinstance(file_obj.metadata, dict)
                            else ""
                        ),
                        "is_processed": official_file.file_type.endswith("_processed"),
                        "edit_meta": (
                            official_file.metadata.get("edit_meta")
                            if isinstance(official_file.metadata, dict)
                            else None
                        ),
                        "url": _serve_file_url(request, official_file.id),
                    }
                )

        if not images_data:
            return JsonResponse(
                {"error": "No intraoral image files found in storage"},
                status=404,
            )

        return JsonResponse({"images": images_data, "count": len(images_data)})

    except Exception as e:
        logger.error(f"Error serving intraoral data: {e}", exc_info=True)
        return JsonResponse({"error": "Internal server error"}, status=500)


@login_required
def patient_teleradiography_data(request, patient_id):
    """API endpoint to serve teleradiography image data"""

    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_read_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)

    # Look for teleradiography file in FileRegistry
    try:
        # Prefer processed file, fallback to raw
        teleradiography_file = (
            patient.files.filter(file_type="teleradiography_processed")
            .order_by("-created_at", "-id")
            .first()
        )

        if not teleradiography_file:
            teleradiography_file = (
                patient.files.filter(file_type="teleradiography_raw")
                .order_by("-created_at", "-id")
                .first()
            )

        if not teleradiography_file:
            return JsonResponse(
                {"error": "Teleradiography image not found"}, status=404
            )

        source_file_id = (
            (teleradiography_file.metadata or {}).get("source_file_id")
            if isinstance(teleradiography_file.metadata, dict)
            else None
        )
        source_file_id = source_file_id or teleradiography_file.id
        if request.GET.get("meta") == "1":
            return JsonResponse(
                {
                    "url": _serve_file_url(request, teleradiography_file.id),
                    "source_file_id": source_file_id,
                    "raw_url": _serve_file_url(request, source_file_id),
                    "is_processed": teleradiography_file.file_type.endswith("_processed"),
                }
            )

        if not artifact_exists(teleradiography_file.file_path):
            return JsonResponse(
                {"error": "Teleradiography image file not found in storage"},
                status=404,
            )

        # Determine content type
        file_ext = os.path.splitext(teleradiography_file.file_path)[1].lower()
        content_type = "image/jpeg" if file_ext in [".jpg", ".jpeg"] else "image/png"

        return streaming_response(
            path_or_key=teleradiography_file.file_path,
            content_type=content_type,
            filename=f"teleradiography_{patient_id}{file_ext}",
            as_attachment=False,
        )

    except Exception as e:
        logger.error(f"Error serving teleradiography data: {e}", exc_info=True)
        return JsonResponse({"error": "Internal server error"}, status=500)


@login_required
@require_POST
def save_rgb_image_edit(request, patient_id):
    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_write_patient(request, patient):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    modality_slug = (request.POST.get("modality_slug") or "").strip()
    source_file_id = request.POST.get("source_file_id")
    edited_image = request.FILES.get("image")
    edit_meta_raw = request.POST.get("edit_meta") or "{}"

    modality_to_types = {
        "intraoral-photo": ("intraoral_raw", "intraoral-photo_processed"),
        "teleradiography": ("teleradiography_raw", "teleradiography_processed"),
        "panoramic": ("panoramic_raw", "panoramic_processed"),
    }
    if modality_slug not in modality_to_types:
        return JsonResponse({"success": False, "error": "Unsupported modality"}, status=400)
    if not source_file_id:
        return JsonResponse({"success": False, "error": "source_file_id is required"}, status=400)
    try:
        source_file_id = int(source_file_id)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid source_file_id"}, status=400)
    if not edited_image:
        return JsonResponse({"success": False, "error": "Edited image is required"}, status=400)

    try:
        edit_meta = json.loads(edit_meta_raw)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid edit metadata"}, status=400)

    raw_type, processed_type = modality_to_types[modality_slug]
    source_file = get_object_or_404(FileRegistry, id=source_file_id, patient=patient)
    if source_file.file_type != raw_type:
        return JsonResponse({"success": False, "error": "Source file type mismatch"}, status=400)

    ext = os.path.splitext(edited_image.name or "")[1].lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
        ext = ".png"
    now = timezone.now()
    object_key = (
        f"maxillo/processed/{modality_slug}/{modality_slug}_patient_{patient.patient_id}"
        f"_{source_file_id}_{now.strftime('%Y%m%d%H%M%S')}{ext}"
    )

    fd, tmp_path = tempfile.mkstemp(prefix="tf_rgb_edit_", suffix=ext)
    os.close(fd)
    hash_sha256 = hashlib.sha256()
    file_size = 0
    output_width = None
    output_height = None
    try:
        with open(tmp_path, "wb+") as destination:
            for chunk in edited_image.chunks():
                destination.write(chunk)
                hash_sha256.update(chunk)
                file_size += len(chunk)
        with Image.open(tmp_path) as saved_image:
            output_width, output_height = saved_image.size
        get_object_storage().upload_file(tmp_path, key=object_key)
    except Exception as exc:
        logger.error("Failed to store processed RGB image: %s", exc, exc_info=True)
        return JsonResponse({"success": False, "error": "Failed to save processed image"}, status=500)
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    # Keep one processed file per source image (replace older rows + files)
    old_entries = patient.files.filter(
        file_type=processed_type,
        metadata__source_file_id=source_file_id,
    )
    for row in old_entries:
        try:
            get_object_storage().delete(row.file_path)
        except Exception:
            logger.warning("Failed deleting old processed object %s", row.file_path)
    old_entries.delete()

    modality_fk = Modality.objects.filter(slug=modality_slug).first()
    metadata = dict(source_file.metadata or {})
    if output_width and output_height:
        metadata["image_width"] = output_width
        metadata["image_height"] = output_height
    metadata.update({
        "source_file_id": source_file_id,
        "source_file_type": source_file.file_type,
        "modality_slug": modality_slug,
        "edited_at": now.isoformat(),
        "edited_by": request.user.username,
        "edit_meta": edit_meta,
    })

    processed_row = FileRegistry.objects.create(
        file_type=processed_type,
        file_path=object_key,
        file_size=file_size,
        file_hash=hash_sha256.hexdigest(),
        metadata=metadata,
        modality=modality_fk,
        domain="maxillo",
        patient=patient,
    )

    return JsonResponse({
        "success": True,
        "file_id": processed_row.id,
        "url": _serve_file_url(request, processed_row.id),
        "processed_file_type": processed_type,
    })
