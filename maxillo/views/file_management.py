"""Patient file management (raw add/remove) views."""

import hashlib
import logging
import os
import tempfile

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required

from common.models import FileRegistry, Job, Modality
from common.models import Project
from common.permissions import user_can_write_annotations, user_is_project_admin
from common.object_storage import get_object_storage

from .domain import get_domain_models, get_namespace
from ..file_utils import get_file_type_for_modality

logger = logging.getLogger(__name__)


def _allowed_modalities_for_request(request):
    cp_id = request.session.get("current_project_id")
    if cp_id:
        proj = Project.objects.filter(id=cp_id).prefetch_related("modalities").first()
        if proj:
            return list(proj.modalities.filter(is_active=True))
    return list(Modality.objects.filter(is_active=True))


def _raw_type_map_for_modalities(modalities):
    result = {}
    for modality in modalities:
        slug = (getattr(modality, "slug", "") or "").strip()
        if not slug:
            continue

        subtypes = [str(s).strip() for s in (getattr(modality, "subtypes", None) or []) if str(s).strip()]
        if slug == "ios" and not subtypes:
            subtypes = ["upper", "lower"]

        if subtypes:
            for subtype in subtypes:
                raw_type = get_file_type_for_modality(slug, is_processed=False, subtype=subtype)
                if raw_type and "_raw" in raw_type:
                    result[raw_type] = {"modality_slug": slug, "subtype": subtype}
        else:
            raw_type = get_file_type_for_modality(slug, is_processed=False)
            if raw_type and "_raw" in raw_type:
                result[raw_type] = {"modality_slug": slug, "subtype": None}
    return result


def _processed_types_for_raw(raw_type):
    valid_file_types = set(FileRegistry.get_file_type_choices_dict().keys())
    if not raw_type or "_raw" not in raw_type:
        return []
    processed_type = raw_type.replace("_raw", "_processed")
    if processed_type in valid_file_types:
        return [processed_type]
    return []


def _safe_metadata_dict(value):
    return value if isinstance(value, dict) else {}


def _job_filter(domain, patient):
    if domain == "brain":
        return {"domain": "brain", "brain_patient": patient}
    return {"domain": "maxillo", "patient": patient}


def _patient_pk(patient):
    return getattr(patient, "pk", None) or getattr(patient, "patient_id", None) or getattr(patient, "id", None)


def _remove_value_recursive(container, value_to_remove):
    if isinstance(container, dict):
        cleaned = {}
        for k, v in container.items():
            if v == value_to_remove:
                continue
            cleaned[k] = _remove_value_recursive(v, value_to_remove)
        return cleaned
    if isinstance(container, list):
        return [_remove_value_recursive(v, value_to_remove) for v in container if v != value_to_remove]
    return container


def _mark_job_failed_after_input_change(job, msg):
    job.status = "failed"
    job.started_at = None
    job.completed_at = None
    job.worker_id = ""
    job.error_logs = msg
    job.save(update_fields=["status", "started_at", "completed_at", "worker_id", "error_logs", "input_files"])


def _append_input_for_file(job, file_type, file_path, file_id):
    inputs = dict(job.input_files or {})
    if file_type == "ios_raw_upper":
        inputs["upper"] = file_path
    elif file_type == "ios_raw_lower":
        inputs["lower"] = file_path
    elif file_type == "intraoral_raw":
        inputs[str(file_id)] = file_path
    else:
        inputs["input"] = file_path
    job.input_files = inputs


def _upload_file_to_storage(storage_key, uploaded_file):
    fd, tmp_path = tempfile.mkstemp(prefix="tf_file_mgmt_")
    os.close(fd)
    hash_sha256 = hashlib.sha256()
    size = 0
    try:
        with open(tmp_path, "wb+") as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)
                hash_sha256.update(chunk)
                size += len(chunk)
        get_object_storage().upload_file(tmp_path, key=storage_key)
        return size, hash_sha256.hexdigest()
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


@login_required
@require_POST
def add_raw_file(request, patient_id):
    Patient = get_domain_models(request)["Patient"]
    domain = get_namespace(request)
    patient = get_object_or_404(Patient, patient_id=patient_id)

    can_modify = bool(patient.folder and user_can_write_annotations(request.user, patient.folder, request))
    if user_is_project_admin(request.user, request):
        can_modify = True
    if not can_modify:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    file_type = (request.POST.get("file_type") or "").strip()
    uploaded = request.FILES.get("file")
    if not file_type or "_raw" not in file_type:
        return JsonResponse({"success": False, "error": "A valid raw file type is required"}, status=400)
    if not uploaded:
        return JsonResponse({"success": False, "error": "No file uploaded"}, status=400)

    raw_type_map = _raw_type_map_for_modalities(_allowed_modalities_for_request(request))
    raw_type_info = raw_type_map.get(file_type)
    if not raw_type_info:
        return JsonResponse({"success": False, "error": "Unsupported raw file type"}, status=400)
    modality_slug = raw_type_info["modality_slug"]

    ext = os.path.splitext(uploaded.name or "")[1] or ".bin"
    key = f"{domain}/raw/{modality_slug}/{modality_slug}_patient_{patient.patient_id}_{timezone.now().strftime('%Y%m%d%H%M%S')}{ext}"
    try:
        file_size, file_hash = _upload_file_to_storage(key, uploaded)
    except Exception as exc:
        logger.error("Failed to upload raw file: %s", exc, exc_info=True)
        return JsonResponse({"success": False, "error": "Failed to upload file"}, status=500)

    modality_fk = Modality.objects.filter(slug=modality_slug).first()
    entity_kwargs = {"domain": domain, "modality": modality_fk}
    if domain == "brain":
        entity_kwargs.update({"brain_patient": patient, "patient": None})
    else:
        entity_kwargs.update({"patient": patient, "brain_patient": None})

    fr = FileRegistry.objects.create(
        file_type=file_type,
        file_path=key,
        file_size=file_size,
        file_hash=file_hash,
        metadata={
            "original_filename": uploaded.name,
            "uploaded_at": timezone.now().isoformat(),
            "modality_slug": modality_slug,
            "subtype": raw_type_info.get("subtype"),
            "uploaded_from": "file_management",
        },
        subtype=raw_type_info.get("subtype") or "",
        **entity_kwargs,
    )

    latest_job = (
        Job.objects.filter(modality_slug=modality_slug, **_job_filter(domain, patient))
        .order_by("-created_at")
        .first()
    )
    if latest_job:
        _append_input_for_file(latest_job, file_type, key, fr.id)
        _mark_job_failed_after_input_change(
            latest_job,
            f"Raw file added from File Management ({file_type}). Job invalidated for rerun.",
        )

    return JsonResponse({
        "success": True,
        "file_id": fr.id,
        "message": "Raw file added successfully",
        "job_updated": bool(latest_job),
    })


@login_required
@require_POST
def delete_raw_file(request, patient_id, file_id):
    Patient = get_domain_models(request)["Patient"]
    domain = get_namespace(request)
    patient = get_object_or_404(Patient, patient_id=patient_id)

    can_modify = bool(patient.folder and user_can_write_annotations(request.user, patient.folder, request))
    if user_is_project_admin(request.user, request):
        can_modify = True
    if not can_modify:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    try:
        raw_file = get_object_or_404(FileRegistry, id=file_id)
        if "_raw" not in str(raw_file.file_type or ""):
            return JsonResponse({"success": False, "error": "Only raw files can be removed here"}, status=400)

        patient_pk = _patient_pk(patient)
        if domain == "brain":
            if raw_file.brain_patient_id != patient_pk:
                return JsonResponse({"success": False, "error": "File does not belong to this patient"}, status=400)
        else:
            if raw_file.patient_id != patient_pk:
                return JsonResponse({"success": False, "error": "File does not belong to this patient"}, status=400)

        metadata = _safe_metadata_dict(getattr(raw_file, "metadata", None))
        modality_slug = (
            (getattr(getattr(raw_file, "modality", None), "slug", "") or "").strip()
            or (metadata.get("modality_slug") or "").strip()
        )
        related_processed_types = _processed_types_for_raw(raw_file.file_type)

        entity_filter = _job_filter(domain, patient)
        if related_processed_types:
            processed_qs = FileRegistry.objects.filter(file_type__in=related_processed_types, **entity_filter)
            processed_deleted = processed_qs.count()
            processed_qs.delete()
        else:
            processed_deleted = 0

        latest_job = None
        if modality_slug:
            latest_job = (
                Job.objects.filter(modality_slug=modality_slug, **entity_filter)
                .order_by("-created_at")
                .first()
            )
        if latest_job:
            latest_job.input_files = _remove_value_recursive(latest_job.input_files or {}, raw_file.file_path)
            _mark_job_failed_after_input_change(
                latest_job,
                f"Raw file removed from File Management ({raw_file.file_type}). Job invalidated for rerun.",
            )

        raw_file.delete()
        return JsonResponse({
            "success": True,
            "message": "Raw file removed successfully",
            "processed_deleted": processed_deleted,
            "job_updated": bool(latest_job),
        })
    except Exception as exc:
        logger.error("Failed deleting raw file %s for patient %s: %s", file_id, patient_id, exc, exc_info=True)
        return JsonResponse({"success": False, "error": str(exc)}, status=500)
