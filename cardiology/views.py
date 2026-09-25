"""Cardiology views: the patient pages, ECG upload, the rhythm call and the plot.

Everything that is the same in every domain -- folders, tags, captions, deletion,
exports, the profile page -- is served from ``common/domain_views`` (see
``app_urls.py``). ECG plotting is client-side (``static/js/cardiology/ecg_plot.js``);
there is no processing pipeline, so no job or modality-status endpoints.
"""

import hashlib
import io
import json as _json
import logging
import os
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from annotations.queries import with_ecg_rhythm
from annotations.services.ecg_rhythm import (
    RHYTHM_CHOICES,
    RHYTHM_METHOD_SLUG,
    ecg_rhythm_state,
    save_ecg_rhythm,
)
from annotations.services.exceptions import AnnotationConflict, AnnotationNotAllowed
from common.activity import log_activity, record_recent
from common.file_access import authorize_file_read, streaming_response
from common.models import FileRegistry, Project, ProjectAccess
from common.object_storage import get_object_storage
from common.permissions import current_project as session_project
from common.permissions import (
    filter_folders_for_user,
    filter_patients_for_user,
    get_patient_for,
    user_can_delete_single_patient,
    user_can_edit_caption,
    user_can_view_caption_content,
    user_can_write_annotations,
    user_can_write_patient_annotations,
    user_is_project_admin,
)
from common.project_filters import presence_filter_specs
from common.rerun import bulk_rerun_steps, describe, rerun_steps_for_patient
from common.uploads import processed_key_prefix_for
from common.view_helpers import (
    bulk_upload_url_for,
    frameable_by_same_origin,
    patient_list_response,
    redirect_with_namespace,
    render_with_fallback,
    upload_error_response,
    wants_json,
)

from .file_utils import PLOT_MAX_PIXELS, InvalidEcgFile, save_ecg_to_dataset
from .forms import PatientForm, PatientManagementForm, PatientUploadForm
from .models import Folder, Patient, Tag

logger = logging.getLogger(__name__)

NAMESPACE = "cardiology"


def home(request):
    return redirect("cardiology:patient_list")


@login_required
def select_project(request, project_id):
    project = get_object_or_404(Project, id=project_id, is_active=True)
    if not user_is_project_admin(request.user, project):
        has_access = ProjectAccess.objects.filter(user=request.user, project=project).exists()
        if not has_access:
            messages.error(request, f"You don't have access to the {project.name} project.")
            return redirect("home")
    request.session["current_project_id"] = project.id
    return redirect("cardiology:patient_list")


def _next_patient_id(patient):
    """The next patient down the list from this one, so "Next" walks an
    annotator through a batch in the same order ``patient_list`` shows it.

    Scoped to the current folder when the patient has one (annotators work
    folder-by-folder through a batch), falling back to the project's unfiled
    patients. ``patient_list`` orders by ``-uploaded_at``; matching that here is
    what makes "Next" mean next rather than "next by higher id".
    """
    if patient.folder_id:
        candidates = Patient.objects.filter(folder_id=patient.folder_id)
    else:
        candidates = Patient.objects.filter(project=patient.project, folder__isnull=True)
    next_patient = (
        candidates.filter(
            Q(uploaded_at__lt=patient.uploaded_at)
            | Q(uploaded_at=patient.uploaded_at, patient_id__lt=patient.patient_id)
        )
        .order_by("-uploaded_at", "-patient_id")
        .first()
    )
    return next_patient.patient_id if next_patient else None


@login_required
@frameable_by_same_origin
def patient_detail(request, patient_id):
    # Framed by the ECG warmup page, which drives this view in a hidden iframe.
    patient = get_patient_for(request.user, Patient, patient_id, "read")
    can_modify = user_can_write_patient_annotations(request.user, patient)
    management_form = PatientManagementForm(instance=patient, user=request.user)

    if request.method == "POST" and can_modify and request.POST.get("action") == "update_management":
        management_form = PatientManagementForm(request.POST, instance=patient, user=request.user)
        if management_form.is_valid():
            management_form.save()
            messages.success(request, "Recording settings updated successfully!")
            return redirect("cardiology:patient_detail", patient_id=patient_id)

    ecg_file = patient.get_ecg_raw_file()
    ecg_data = None
    if ecg_file is not None:
        ecg_data = {
            "url": reverse("cardiology:api_serve_file", kwargs={"file_id": ecg_file.id}),
            "filename": (ecg_file.metadata or {}).get("original_filename", "ecg.json"),
            "saveUrl": reverse("cardiology:save_browser_ecg_plot", kwargs={"patient_id": patient.patient_id}),
            "hasProcessed": has_browser_ecg_plot(patient),
        }

    voice_captions = patient.voice_captions.all()
    for caption in voice_captions:
        caption.can_view_content = user_can_view_caption_content(request.user, caption)
        caption.can_edit_content = user_can_edit_caption(request.user, caption)
        caption.is_ghost = not caption.can_view_content

    allowed_annotations = list(
        patient.project.annotation_methods.filter(is_active=True).values_list("slug", flat=True)
    )
    classification_enabled = RHYTHM_METHOD_SLUG in allowed_annotations
    captions_enabled = "voice_caption" in allowed_annotations

    context = {
        "patient": patient,
        "user_profile": request.user.profile,
        "management_form": management_form,
        "can_modify": can_modify,
        "can_modify_segmentation": can_modify,
        "can_create_caption": can_modify,
        "ecg_data": ecg_data,
        "rhythm": ecg_rhythm_state(patient),
        "rhythm_choices": RHYTHM_CHOICES,
        "voice_captions": voice_captions,
        "is_admin_user": user_is_project_admin(request.user, patient.project),
        "allowed_annotations": allowed_annotations,
        "classification_enabled": classification_enabled,
        "captions_enabled": captions_enabled,
        "default_tab": "classification" if classification_enabled or not captions_enabled else "captions",
        "next_patient_id": _next_patient_id(patient),
    }

    record_recent(
        request.user, NAMESPACE, patient.patient_id,
        patient_name=patient.name or "",
        project_label=patient.project.name,
    )
    return render_with_fallback(request, "patient_detail", context)


@login_required
def patient_list(request):
    patients = Patient.objects.select_related("uploaded_by", "folder").prefetch_related(
        "voice_captions", "voice_captions__user",
    )
    current_project = session_project(request)
    patients = patients.filter(project=current_project)
    patients = filter_patients_for_user(request.user, patients, NAMESPACE)
    patients_for_folder_counts = patients

    search_query = request.GET.get("search", "").strip()
    if search_query:
        patients = patients.filter(Q(name__icontains=search_query) | Q(patient_id__icontains=search_query))

    folder_id = request.GET.get("folder")
    if folder_id and folder_id != "all":
        if folder_id == "root":
            patients = patients.filter(folder__isnull=True)
        else:
            try:
                patients = patients.filter(folder_id=int(folder_id))
            except ValueError:
                pass

    tags_selected = request.GET.getlist("tags")
    if tags_selected:
        patients = patients.filter(tags__name__in=tags_selected).distinct()

    has_reports_filter = request.GET.get("has_reports", "")
    if has_reports_filter == "yes":
        patients = patients.filter(voice_captions__isnull=False).distinct()

    has_classification_filter = request.GET.get("has_ecg_classification", "")
    if has_classification_filter == "yes":
        patients = with_ecg_rhythm(patients)

    # Not one of the generic PRESENCE_FILTERS (common/project_filters.py): "neither a
    # caption nor a rhythm call" spans two stores, which that mechanism cannot express.
    no_annotations_filter = request.GET.get("no_annotations", "")
    if no_annotations_filter == "yes":
        patients = patients.filter(voice_captions__isnull=True).exclude(
            patient_id__in=with_ecg_rhythm(Patient.objects.all()).values("patient_id")
        )

    patients = patients.order_by("-uploaded_at")

    try:
        per_page = int(request.GET.get("per_page", 10))
    except (TypeError, ValueError):
        per_page = 10
    if per_page not in {10, 20, 50, 100}:
        per_page = 10
    page_obj = Paginator(patients, per_page).get_page(request.GET.get("page"))

    is_admin = user_is_project_admin(request.user, current_project)
    page_rows = []
    for patient in page_obj.object_list:
        voice_captions = list(patient.voice_captions.all())
        page_rows.append({
            "patient": patient,
            "voice_caption_count": len(voice_captions),
            "voice_annotators": list({vc.user.username for vc in voice_captions}),
            "folder": patient.folder,
            "can_delete": bool(
                is_admin
                or (patient.folder and user_can_delete_single_patient(request.user, patient.folder, patient.project))
            ),
        })
    page_obj.object_list = page_rows

    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(parent__isnull=True, project=current_project).order_by("name"),
        NAMESPACE,
    )
    projects_for_sidebar = Project.objects.filter(domain=NAMESPACE, is_active=True)
    if not request.user.is_staff:
        accessible_project_ids = ProjectAccess.objects.filter(user=request.user).values_list("project_id", flat=True)
        projects_for_sidebar = projects_for_sidebar.filter(id__in=accessible_project_ids)

    allowed_modality_slugs = list(current_project.modalities.values_list("slug", flat=True)) if current_project else []
    filter_specs = presence_filter_specs(request, current_project, allowed_modality_slugs)
    enabled_methods = set(current_project.annotation_methods.values_list("slug", flat=True)) if current_project else set()
    if {"voice_caption", RHYTHM_METHOD_SLUG} & enabled_methods:
        filter_specs = filter_specs + [{
            "key": "presence_no_annotations",
            "param": "no_annotations",
            "label": "No annotations",
            "icon": "fas fa-circle-xmark",
            "value": no_annotations_filter,
        }]

    context = {
        "page_obj": page_obj,
        "current_project_id": current_project.id if current_project else None,
        "projects": projects_for_sidebar.order_by("name"),
        "search_query": search_query,
        "folder_id": folder_id or "all",
        "selected_tags": tags_selected,
        "folders": [
            {"folder": folder, "patient_count": patients_for_folder_counts.filter(folder=folder).count()}
            for folder in folders
        ],
        "all_tags": Tag.objects.all().order_by("name"),
        "per_page": per_page,
        "user_profile": request.user.profile,
        "is_admin_user": is_admin,
        "has_reports_filter": has_reports_filter,
        "has_ecg_classification_filter": has_classification_filter,
        "no_annotations_filter": no_annotations_filter,
        "presence_filter_specs": filter_specs,
    }
    return render_with_fallback(request, "patient_list", context)


def _first_form_error(form):
    for message in form.non_field_errors():
        return message
    for field, errors in form.errors.items():
        if errors:
            label = form.fields[field].label if field in form.fields else field
            return f"{label}: {errors[0]}"
    return "Upload failed. Please check the form and try again."


@login_required
def upload_patient(request):
    user_profile = request.user.profile
    if not user_profile or not user_profile.can_upload_scans():
        messages.error(request, "You do not have permission to upload recordings.")
        return redirect_with_namespace(request, "patient_list")

    project = session_project(request)
    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(parent__isnull=True, project=project).order_by("name"),
        NAMESPACE,
    )
    allowed_modalities = list(project.modalities.filter(is_active=True)) if project else []

    def render_form(form, status=200):
        return render(request, "common/upload/upload.html", {
            "patient_form": PatientForm(),
            "patient_upload_form": form,
            "folders": folders,
            "allowed_modalities": allowed_modalities,
            "bulk_upload_url": bulk_upload_url_for(request, NAMESPACE),
        }, status=status)

    if request.method != "POST":
        return render_form(PatientUploadForm(user=request.user, current_project=project))

    form = PatientUploadForm(request.POST, request.FILES, user=request.user, current_project=project)
    if not form.is_valid():
        failed = upload_error_response(request, _first_form_error(form))
        return failed if failed is not None else render_form(form)

    # The form checked the folder belongs to the chosen project; whether this user
    # may add patients there is a question about that project, not the session's.
    target_project = form.cleaned_data["project"]
    folder = form.cleaned_data["folder"]
    if not user_can_write_annotations(request.user, folder, target_project):
        denied = "You do not have permission to upload to the selected folder."
        failed = upload_error_response(request, denied, status=403)
        if failed is not None:
            return failed
        messages.error(request, denied)
        return render_form(form, status=403)

    ecg_file = form.cleaned_data["ecg"]
    try:
        with transaction.atomic():
            patient = form.save(commit=False)
            patient.uploaded_by = request.user
            patient.project = target_project
            patient.folder = folder
            patient.save()
            form.instance = patient
            form.save(commit=True)
            modality = target_project.modalities.filter(slug="ecg").first()
            if modality:
                patient.modalities.add(modality)
            save_ecg_to_dataset(patient, ecg_file)
    except InvalidEcgFile as exc:
        # validate_ecg_json already ran in the form; this is the same check, kept
        # as the last word before bytes reach storage.
        failed = upload_error_response(request, str(exc))
        if failed is not None:
            return failed
        messages.error(request, str(exc))
        return render_form(form)

    messages.success(request, "Patient uploaded successfully!")
    return patient_list_response(request)


@login_required
def bulk_upload_patients(request):
    """Create one patient per uploaded ECG file, all into one folder.

    Mirrors ``maxillo/views/patient_upload.py::bulk_upload_patients``, trimmed
    for a single modality: every file is handed to ``save_ecg_to_dataset`` and
    reported per file (one bad file costs only its own row).
    """
    project = session_project(request)

    # Bulk ingestion creates patients wholesale and bypasses per-case review, so
    # it is administrators only (the single-patient upload stays open to annotators).
    if project is None or not user_is_project_admin(request.user, project):
        message = "Bulk upload is restricted to project administrators."
        if wants_json(request):
            return JsonResponse({"ok": False, "error": message}, status=403)
        messages.error(request, message)
        return redirect_with_namespace(request, "patient_list")

    ecg_modality = project.modalities.filter(slug="ecg", is_active=True).first()
    folders = filter_folders_for_user(
        request.user, Folder.objects.filter(project=project).order_by("name"), NAMESPACE,
    )

    if request.method == "GET":
        return render(request, "common/upload/bulk_upload.html", {
            "current_project": project,
            "folders": folders,
            "allowed_modalities": [ecg_modality] if ecg_modality else [],
            "accept_attribute": ".json",
        })

    uploaded_files = request.FILES.getlist("files")
    if not uploaded_files:
        return _bulk_response(request, [], "Select at least one file to upload.")

    folder = None
    folder_id = request.POST.get("folder")
    if folder_id:
        folder = next((f for f in folders if str(f.id) == str(folder_id)), None)
    if folder is None:
        return _bulk_response(request, [], "Choose a folder of this project to upload into.")

    results = [
        _bulk_upload_one_ecg(request, project, folder, ecg_modality, uploaded_file)
        for uploaded_file in uploaded_files
    ]
    return _bulk_response(request, results)


def _patient_name_from_filename(filename):
    """Patient name for a bulk-uploaded file: its basename without the extension."""
    base = os.path.basename((filename or "").replace("\\", "/")).strip()
    return os.path.splitext(base)[0].strip()[:100]


def _bulk_upload_one_ecg(request, project, folder, modality, uploaded_file):
    """Create one patient for one ECG file. Never raises; reports the outcome."""
    filename = getattr(uploaded_file, "name", "") or "file"
    if not filename.lower().endswith(".json"):
        return {"file": filename, "ok": False, "error": "unsupported file type (expected .json)"}

    try:
        with transaction.atomic():
            patient = Patient(
                name=_patient_name_from_filename(filename),
                project=project,
                folder=folder,
                uploaded_by=request.user,
            )
            patient.save()
            if modality:
                patient.modalities.add(modality)
            save_ecg_to_dataset(patient, uploaded_file)
    except InvalidEcgFile as exc:
        return {"file": filename, "ok": False, "error": str(exc)}
    except Exception:  # noqa: BLE001 - reported per file, the batch continues
        logger.exception("Bulk ECG upload failed for %s", filename)
        return {"file": filename, "ok": False, "error": "could not be stored; see the server log"}

    log_activity(
        request.user, NAMESPACE, patient.patient_id,
        patient_name=patient.name, verb="uploaded", target="ecg",
        bulk=True, original_filename=filename,
    )
    return {"file": filename, "ok": True, "patient_id": patient.patient_id, "patient_name": patient.name, "modality": "ecg"}


def _bulk_response(request, results, error=None):
    """JSON for the XHR uploader, messages + redirect for the plain form."""
    created = [item for item in results if item.get("ok")]
    failed = [item for item in results if not item.get("ok")]

    if wants_json(request):
        if error:
            return JsonResponse({"ok": False, "error": error, "results": results}, status=400)
        return JsonResponse(
            {"ok": not failed, "created": len(created), "failed": len(failed), "results": results},
            status=200 if created or not results else 400,
        )

    if error:
        messages.error(request, error)
        return redirect_with_namespace(request, "bulk_upload_patients")
    if created:
        messages.success(request, f"Created {len(created)} patient(s).")
    for item in failed:
        messages.error(request, f"{item['file']}: {item['error']}")
    if created and not failed:
        return redirect_with_namespace(request, "patient_list")
    return redirect_with_namespace(request, "bulk_upload_patients")


@login_required
@require_POST
def update_patient_name(request, patient_id):
    patient = get_patient_for(request.user, Patient, patient_id, "write")
    try:
        data = _json.loads(request.body)
    except _json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    name = (data.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "Name cannot be empty"}, status=400)
    patient.name = name[:100]
    patient.save(update_fields=["name"])
    return JsonResponse({"success": True, "name": patient.name})


@login_required
@require_POST
def rerun_processing(request, patient_id):
    patient = get_patient_for(request.user, Patient, patient_id, "admin")
    try:
        data = _json.loads(request.body) if request.body else {}
    except _json.JSONDecodeError:
        data = {}
    requested_jobs = data.get("jobs") or list(patient.modalities.values_list("slug", flat=True))
    result = rerun_steps_for_patient(patient, requested_jobs)
    return JsonResponse({
        "success": True,
        "message": describe(result),
        "updated": result["updated"],
        "created": result["created"],
        "not_found": result["not_found"],
    })


@login_required
@require_POST
def bulk_rerun_processing(request):
    try:
        data = _json.loads(request.body) if request.body else {}
    except _json.JSONDecodeError:
        data = {}

    scan_ids = data.get("scan_ids", [])
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse({"success": False, "error": "scan_ids list is required"}, status=400)
    requested_jobs = data.get("jobs")
    if not isinstance(requested_jobs, list) or not requested_jobs:
        return JsonResponse({"success": False, "error": "jobs list is required"}, status=400)

    patients = list(Patient.objects.select_related("project").filter(patient_id__in=scan_ids))
    for patient in patients:
        if not user_is_project_admin(request.user, patient.project):
            return JsonResponse(
                {"success": False, "error": f"Permission denied for patient {patient.patient_id}"}, status=403
            )

    result = bulk_rerun_steps(patients, requested_jobs)
    return JsonResponse({
        "success": True,
        "message": f"Reprocessing queued for {result['updated_pairs']} job(s) across {len(patients)} patient(s).",
        "selected_scan_count": len(patients),
        "requested_modalities": result["requested"],
        "updated_pairs": result["updated_pairs"],
        "not_found_pairs": result["not_found_pairs"],
        "created_slugs": result["created_slugs"],
        "updated_by_modality": result["updated_by_modality"],
        "not_found_by_modality": result["not_found_by_modality"],
    })


@login_required
@require_POST
def classification_update(request, patient_id):
    """Record the AF/NSR/Other/NI call, refusing to overwrite a newer one.

    The client sends the ``revision`` it loaded; if somebody saved since, the
    answer is 409 with the current call, and the page asks the user to reload.
    """
    patient = get_patient_for(request.user, Patient, patient_id, "write")
    try:
        data = _json.loads(request.body)
    except _json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    try:
        expected_revision = int(data.get("revision", 0))
    except (TypeError, ValueError):
        return JsonResponse({"error": "revision must be an integer"}, status=400)

    try:
        state = save_ecg_rhythm(
            patient, data.get("value"), author=request.user, expected_revision=expected_revision
        )
    except ValidationError as exc:
        return JsonResponse({"error": " ".join(exc.messages)}, status=400)
    except AnnotationNotAllowed:
        return JsonResponse({"error": "ECG classification is disabled for this project"}, status=403)
    except AnnotationConflict:
        current = ecg_rhythm_state(patient)
        return JsonResponse({
            "error": "Somebody else changed this classification. Reload to see it.",
            "value": current["value"],
            "revision": current["revision"],
        }, status=409)
    return JsonResponse({
        "success": True,
        "value": state["value"],
        "display_value": state["label"],
        "revision": state["revision"],
    })


@login_required
@require_http_methods(["GET"])
def serve_file(request, file_id, filename=None):
    """Stream one cardiology FileRegistry row (the raw ECG JSON, the plot PNG).

    ``filename`` is URL decoration only, as in the other domains' serve views.
    """
    del filename
    file_obj = FileRegistry.objects.filter(id=file_id, domain=NAMESPACE).first()
    allowed, error, status = authorize_file_read(request.user, file_obj, NAMESPACE)
    if not allowed:
        return JsonResponse({"error": error}, status=status)
    if not file_obj.file_path:
        raise Http404("File not found")
    name = (file_obj.metadata or {}).get("original_filename") or os.path.basename(file_obj.file_path)
    content_type = "image/png" if file_obj.file_type == "ecg_processed" else "application/json"
    return streaming_response(path_or_key=file_obj.file_path, content_type=content_type, filename=name)


# ─── Browser-generated ECG plot (mirrors maxillo's browser panoramic) ────────
#
# The clinical-grid PNG is drawn client-side (static/js/cardiology/ecg_plot.js)
# the first time anyone opens the patient, then POSTed back here so it becomes a
# real export artifact instead of something that only ever exists in one browser
# tab. `ecg_warmup`/`ecg_warmup_pending` let an admin force that first-open pass
# for a whole folder -- the mechanism of maxillo/views/panoramic_warmup.py.

ECG_PLOT_ALGORITHM_VERSION = "ecg-plot-canvas-v1"
ECG_PLOT_MAX_REQUEST_BYTES = 5 * 1024 * 1024
ECG_WARMUP_BATCH_LIMIT = 200


def has_browser_ecg_plot(patient):
    return FileRegistry.objects.filter(
        domain=NAMESPACE, cardiology_patient=patient, file_type="ecg_processed",
        metadata__algorithm_version=ECG_PLOT_ALGORITHM_VERSION,
    ).exists()


def _pending_ecg_patients(folder_ids):
    """Patients with a raw ECG but no current-algorithm generated plot yet."""
    return (
        Patient.objects.filter(folder_id__in=folder_ids, files__file_type="ecg_raw")
        .exclude(
            files__file_type="ecg_processed",
            files__metadata__algorithm_version=ECG_PLOT_ALGORITHM_VERSION,
        )
        .distinct()
        .order_by("patient_id")
    )


@login_required
def ecg_warmup(request):
    project = session_project(request)
    if project is None or not user_is_project_admin(request.user, project):
        messages.error(request, "Only project administrators can run this.")
        return redirect_with_namespace(request, "patient_list")
    folders = filter_folders_for_user(
        request.user, Folder.objects.filter(project=project).order_by("name"), NAMESPACE,
    )
    return render(request, "cardiology/ecg_warmup.html", {
        "project": project, "folders": folders, "batch_limit": ECG_WARMUP_BATCH_LIMIT, "ns": NAMESPACE,
    })


@login_required
@require_http_methods(["GET"])
def ecg_warmup_pending(request):
    project = session_project(request)
    if project is None or not user_is_project_admin(request.user, project):
        return JsonResponse({"error": "Permission denied"}, status=403)

    folder = Folder.objects.filter(id=request.GET.get("folder"), project=project).first()
    if folder is None:
        return JsonResponse({"error": "folder is required and must belong to this project"}, status=400)

    pending = list(_pending_ecg_patients([folder.id])[: ECG_WARMUP_BATCH_LIMIT + 1])
    truncated = len(pending) > ECG_WARMUP_BATCH_LIMIT
    pending = pending[:ECG_WARMUP_BATCH_LIMIT]

    return JsonResponse({
        "folder": {"id": folder.id, "name": folder.name},
        "total": len(pending) if not truncated else _pending_ecg_patients([folder.id]).count(),
        "truncated": truncated,
        "patients": [{"id": p.patient_id, "name": p.name} for p in pending],
    })


def _sanitize_plot_png(png_file):
    """Re-encode the upload with Pillow; refuse anything that is not a sane PNG.

    This PNG goes into every export ZIP from here on, so a corrupt or disguised
    file fails here rather than downstream (see maxillo's ``_sanitize_browser_png``).
    """
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(png_file)
        if image.format != "PNG":
            raise ValueError("not a PNG")
        if image.width * image.height > PLOT_MAX_PIXELS:
            raise ValueError("too large")
        image.verify()
        png_file.seek(0)
        image = Image.open(png_file)
        if image.mode not in ("RGB", "RGBA", "L", "LA"):
            image = image.convert("RGBA")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    return buffer.getvalue()


@login_required
@require_POST
def save_browser_ecg_plot(request, patient_id):
    """The browser's one-time POST of the clinical-grid PNG it just rendered."""
    patient = get_patient_for(request.user, Patient, patient_id, "write")

    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return JsonResponse({"error": "Invalid Content-Length"}, status=400)
    if content_length > ECG_PLOT_MAX_REQUEST_BYTES:
        return JsonResponse({"error": "Payload too large"}, status=413)

    raw_ecg = patient.get_ecg_raw_file()
    if raw_ecg is None:
        return JsonResponse({"error": "This patient has no ECG recording to plot."}, status=409)

    png_file = request.FILES.get("plot_png")
    try:
        # It becomes part of an object key, so it must be exactly what it claims to be.
        generation_uuid = str(uuid.UUID(request.POST.get("generation_uuid") or ""))
    except ValueError:
        generation_uuid = None
    if not png_file or not generation_uuid:
        return JsonResponse({"error": "plot_png and a UUID generation_uuid are required"}, status=400)
    if png_file.size > ECG_PLOT_MAX_REQUEST_BYTES:
        return JsonResponse({"error": "Payload too large"}, status=413)

    png_bytes = _sanitize_plot_png(png_file)
    if png_bytes is None:
        return JsonResponse({"error": "plot_png is not a valid PNG of an acceptable size"}, status=400)

    storage = get_object_storage()
    key = f"{processed_key_prefix_for(patient, 'ecg')}/ecg_plot_{patient.patient_id}_{generation_uuid}.png"
    with transaction.atomic():
        # The patient row lock serializes concurrent saves, as save_browser_panoramic does.
        Patient.objects.select_for_update().get(pk=patient.pk)
        existing = (
            FileRegistry.objects.filter(domain=NAMESPACE, cardiology_patient=patient, file_type="ecg_processed")
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            existing_meta = existing.metadata or {}
            if existing_meta.get("generation_uuid") == generation_uuid or (
                existing_meta.get("algorithm_version") == ECG_PLOT_ALGORITHM_VERSION
            ):
                return JsonResponse({"success": True, "idempotent": True, "outcome": "existing"})

        storage.upload_fileobj(io.BytesIO(png_bytes), key=key, content_type="image/png")
        try:
            FileRegistry.objects.create(
                file_type="ecg_processed",
                file_path=key,
                file_size=len(png_bytes),
                file_hash=hashlib.sha256(png_bytes).hexdigest(),
                modality=patient.modalities.filter(slug="ecg").first(),
                domain=NAMESPACE,
                cardiology_patient=patient,
                metadata={
                    "generated_from": "browser_ecg_plot",
                    "algorithm_version": ECG_PLOT_ALGORITHM_VERSION,
                    "generation_uuid": generation_uuid,
                    "source_file_id": raw_ecg.id,
                },
            )
        except Exception:
            # Nothing references the object without its row; do not leave it behind.
            try:
                storage.delete(key)
            except Exception:  # noqa: BLE001 - the original error is the one to report
                logger.exception("Could not remove orphaned ECG plot %s", key)
            raise

    return JsonResponse({"success": True, "idempotent": False, "outcome": "created"})
