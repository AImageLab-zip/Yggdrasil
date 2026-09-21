"""Cardiology views.

Self-contained, like ``brain/views.py`` -- deliberately not routed through
``maxillo/views/domain.py``'s namespace branch (see the project plan for why).
ECG plotting is client-side (``static/js/cardiology/ecg_plot.js``); there is no
processing pipeline, so there are no job/modality-status endpoints here.
"""

import json as _json
import logging
import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponseGone, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from common import export_catalog, export_ui
from common.activity import log_activity
from common.domains import landing_domain_cards, order_projects_for_landing
from common.project_filters import presence_filter_specs
from common.export_processing import (
    ExportProcessor,
    start_export_processing,
    build_shared_download_url as _build_shared_download_url,
    format_file_size,
    kill_export_processes as _kill_export_processes,
    recover_stuck_export as _recover_stuck_export,
)
from common.export_share import is_share_expired, resolve_share_expiry
from common.file_access import authorize_file_read, exists as artifact_exists, streaming_response
from common.models import FileRegistry, Project, ProjectAccess
from common.object_storage import get_object_storage
from common.permissions import (
    filter_folders_for_user,
    filter_patients_for_user,
    project_allows_annotation,
    user_can_delete_single_patient,
    user_can_edit_caption,
    user_can_view_caption_content,
    user_can_write_patient_annotations,
    user_has_project_access,
    user_is_project_admin,
)
from common.view_helpers import (
    bulk_upload_url_for,
    patient_list_response,
    redirect_with_namespace,
    render_with_fallback,
    upload_error_response,
    wants_json,
)

from .file_utils import InvalidEcgFile, save_ecg_to_dataset
from .forms import PatientForm, PatientManagementForm, PatientUploadForm
from .models import Classification, Export, Folder, Patient, Tag, VoiceCaption

logger = logging.getLogger(__name__)


def home(request):
    if request.user.is_authenticated:
        projects = Project.objects.filter(is_active=True)
        if not request.user.is_staff:
            project_ids = ProjectAccess.objects.filter(user=request.user).values_list("project_id", flat=True)
            projects = projects.filter(id__in=project_ids)
        current_project_id = request.session.get("current_project_id")
        current_project_name = None
        if current_project_id:
            current_project = projects.filter(id=current_project_id).first()
            current_project_name = current_project.name if current_project else None
        ordered_projects = order_projects_for_landing(projects)
        return render(request, "common/landing.html", {
            "projects": ordered_projects,
            "landing_cards": landing_domain_cards(request.user),
            "current_project_id": current_project_id,
            "current_project_name": current_project_name,
            "continue_url": "/cardiology/" if current_project_name else None,
        })
    return render(request, "common/landing.html", {})


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
    folder-by-folder through a batch), falling back to the whole project.
    ``patient_list`` orders by ``-uploaded_at``; matching that here is what
    makes "Next" actually mean next, instead of "next by higher id" -- which
    looks disabled immediately if you start from the newest upload at the top
    of the list, the natural place to start.
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
def patient_detail(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    can_view = bool(patient.project and user_has_project_access(request.user, patient.project))
    if user_is_project_admin(request.user, patient.project):
        can_view = True
    if not can_view:
        messages.error(request, "You do not have permission to view this recording.")
        return redirect("cardiology:patient_list")

    management_form = PatientManagementForm(instance=patient, user=request.user)

    can_modify = bool(patient.project and user_can_write_patient_annotations(request.user, patient))
    if user_is_project_admin(request.user, patient.project):
        can_modify = True

    if request.method == "POST" and can_modify:
        action = request.POST.get("action")
        if action == "update_management":
            management_form = PatientManagementForm(request.POST, instance=patient, user=request.user)
            if management_form.is_valid():
                management_form.save()
                messages.success(request, "Recording settings updated successfully!")
                return redirect("cardiology:patient_detail", patient_id=patient_id)

    ecg_file = patient.get_ecg_raw_file()
    ecg_data = None
    if ecg_file is not None:
        ecg_data = {
            "fileId": ecg_file.id,
            "url": reverse("cardiology:serve_ecg_file", kwargs={"patient_id": patient.patient_id, "file_id": ecg_file.id}),
            "filename": (ecg_file.metadata or {}).get("original_filename", "ecg.json"),
            "saveUrl": reverse("cardiology:save_browser_ecg_plot", kwargs={"patient_id": patient.patient_id}),
            "algorithmVersion": ECG_PLOT_ALGORITHM_VERSION,
            "hasProcessed": has_browser_ecg_plot(patient),
        }

    classification = patient.classifications.filter(classifier="manual").first()

    voice_captions = patient.voice_captions.all()
    is_admin_user = user_is_project_admin(request.user, patient.project)
    for caption in voice_captions:
        caption.can_view_content = user_can_view_caption_content(request.user, caption)
        caption.can_edit_content = user_can_edit_caption(request.user, caption)
        caption.is_ghost = not caption.can_view_content

    allowed_annotations = []
    if patient.project is not None:
        allowed_annotations = list(
            patient.project.annotation_methods.filter(is_active=True).values_list("slug", flat=True)
        )

    context = {
        "patient": patient,
        "user_profile": request.user.profile,
        "management_form": management_form,
        "can_modify": can_modify,
        "can_create_caption": can_modify,
        "ecg_data": ecg_data,
        "classification": classification,
        "classification_choices": Classification.VALUE_CHOICES,
        "voice_captions": voice_captions,
        "is_admin_user": is_admin_user,
        "allowed_annotations": allowed_annotations,
        "classification_enabled": "ecg_classification" in allowed_annotations,
        "next_patient_id": _next_patient_id(patient),
    }

    from common.activity import record_recent
    record_recent(
        request.user, "cardiology", patient.patient_id,
        patient_name=getattr(patient, "name", "") or "",
        project_label="Cardiology",
    )
    return render_with_fallback(request, "patient_detail", context)


@login_required
def patient_list(request):
    patients = Patient.objects.select_related("uploaded_by").prefetch_related(
        "voice_captions", "voice_captions__user", "tags", "files",
    )
    current_project_id = request.session.get("current_project_id")
    current_project = None
    if current_project_id:
        patients = patients.filter(project_id=current_project_id)
        current_project = Project.objects.filter(id=current_project_id).prefetch_related("annotation_methods").first()
    patients = filter_patients_for_user(request.user, patients, "cardiology")
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
        patients = patients.filter(classifications__classifier="manual").distinct()

    # Not one of the generic per-method PRESENCE_FILTERS (common/project_filters.py):
    # it's a compound "neither" condition spanning two unrelated tables, which that
    # framework has no way to express, so it's appended to this view's own spec list
    # below instead of the shared one -- see the comment there for why it must not be.
    no_annotations_filter = request.GET.get("no_annotations", "")
    if no_annotations_filter == "yes":
        patients = patients.filter(voice_captions__isnull=True).exclude(classifications__classifier="manual")

    patients = patients.order_by("-uploaded_at")

    is_admin = user_is_project_admin(request.user, request)
    patients_with_status = []
    for patient in patients:
        voice_captions = list(patient.voice_captions.all())
        classification = patient.classifications.filter(classifier="manual").first()
        patients_with_status.append({
            "patient": patient,
            "has_ecg": patient.has_ecg(),
            "classification_value": classification.value if classification else "",
            "voice_caption_count": len(voice_captions),
            "voice_annotators": list({vc.user.username for vc in voice_captions}),
            "tags": patient.tag_names(),
            "folder": patient.folder,
            "can_delete": bool(is_admin or (patient.folder and user_can_delete_single_patient(request.user, patient.folder, patient.project))),
        })

    try:
        per_page = int(request.GET.get("per_page", 10))
    except (TypeError, ValueError):
        per_page = 10
    if per_page not in {10, 20, 50, 100}:
        per_page = 10
    page_obj = Paginator(patients_with_status, per_page).get_page(request.GET.get("page"))

    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(parent__isnull=True)
        .filter(project_id=current_project_id if current_project_id else None)
        .order_by("name"),
        "cardiology",
    )
    projects_for_sidebar = Project.objects.filter(domain="cardiology", is_active=True)
    if not request.user.is_staff:
        accessible_project_ids = ProjectAccess.objects.filter(user=request.user).values_list("project_id", flat=True)
        projects_for_sidebar = projects_for_sidebar.filter(id__in=accessible_project_ids)

    allowed_modality_slugs = list(current_project.modalities.values_list("slug", flat=True)) if current_project else []
    filter_specs = presence_filter_specs(request, current_project, allowed_modality_slugs)
    enabled_methods = set(current_project.annotation_methods.values_list("slug", flat=True)) if current_project else set()
    if {"voice_caption", "ecg_classification"} & enabled_methods:
        filter_specs = filter_specs + [{
            "key": "presence_no_annotations",
            "param": "no_annotations",
            "label": "No annotations",
            "icon": "fas fa-circle-xmark",
            "value": no_annotations_filter,
        }]

    context = {
        "page_obj": page_obj,
        "current_project_id": current_project_id,
        "projects": projects_for_sidebar.order_by("name"),
        "search_query": search_query,
        "folder_id": folder_id or "all",
        "selected_tags": tags_selected,
        "folders": [{"folder": folder, "patient_count": patients_for_folder_counts.filter(folder=folder).count()} for folder in folders],
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
    namespace = "cardiology"

    if not user_profile or not user_profile.can_upload_scans():
        messages.error(request, "You do not have permission to upload recordings.")
        return redirect_with_namespace(request, "patient_list")

    current_project_id = request.session.get("current_project_id")
    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(parent__isnull=True)
        .filter(project_id=current_project_id if current_project_id else None)
        .order_by("name"),
        namespace,
    )
    project = None
    allowed_modalities = []
    if current_project_id:
        try:
            project = Project.objects.prefetch_related("modalities").get(id=current_project_id)
            allowed_modalities = list(project.modalities.filter(is_active=True))
        except Project.DoesNotExist:
            pass

    if request.method == "POST":
        patient_upload_form = PatientUploadForm(
            request.POST, request.FILES, user=request.user, current_project=project
        )
        patient_form = PatientForm()

        ecg_file = request.FILES.get("ecg")
        form_is_valid = patient_upload_form.is_valid()
        if form_is_valid and not ecg_file:
            patient_upload_form.add_error(None, "Add an ECG JSON file before uploading.")
            form_is_valid = False

        if not form_is_valid:
            failed = upload_error_response(request, _first_form_error(patient_upload_form))
            if failed is not None:
                return failed
            return render(request, "common/upload/upload.html", {
                "patient_form": patient_form,
                "patient_upload_form": patient_upload_form,
                "folders": folders,
                "allowed_modalities": allowed_modalities,
                "bulk_upload_url": bulk_upload_url_for(request, namespace),
            })

        patient = patient_upload_form.save(commit=False)
        patient.uploaded_by = request.user

        folder = patient_upload_form.cleaned_data.get("folder")
        if folder:
            allowed_folder_ids = set(
                filter_folders_for_user(
                    request.user, Folder.objects.filter(parent__isnull=True).only("id"), namespace,
                ).values_list("id", flat=True)
            )
            if folder.id not in allowed_folder_ids:
                denied = "You do not have permission to upload to the selected folder."
                failed = upload_error_response(request, denied, status=403)
                if failed is not None:
                    return failed
                messages.error(request, denied)
                return render(request, "common/upload/upload.html", {
                    "patient_form": patient_form,
                    "patient_upload_form": patient_upload_form,
                    "folders": folders,
                    "allowed_modalities": allowed_modalities,
                    "bulk_upload_url": bulk_upload_url_for(request, namespace),
                })

        if project:
            patient.project = project
        if folder:
            patient.folder = folder
        patient.save()
        patient_upload_form.instance = patient
        patient_upload_form.save(commit=True)

        try:
            save_ecg_to_dataset(patient, ecg_file)
        except InvalidEcgFile as exc:
            patient.delete()
            failed = upload_error_response(request, str(exc))
            if failed is not None:
                return failed
            messages.error(request, str(exc))
            return render(request, "common/upload/upload.html", {
                "patient_form": patient_form,
                "patient_upload_form": PatientUploadForm(user=request.user, current_project=project),
                "folders": folders,
                "allowed_modalities": allowed_modalities,
                "bulk_upload_url": bulk_upload_url_for(request, namespace),
            })

        messages.success(request, "Patient uploaded successfully!")
        return patient_list_response(request)

    patient_form = PatientForm()
    patient_upload_form = PatientUploadForm(user=request.user, current_project=project)
    return render(request, "common/upload/upload.html", {
        "patient_form": patient_form,
        "patient_upload_form": patient_upload_form,
        "folders": folders,
        "allowed_modalities": allowed_modalities,
        "bulk_upload_url": bulk_upload_url_for(request, namespace),
    })


@login_required
def bulk_upload_patients(request):
    """Create one patient per uploaded ECG file, all into one folder.

    Mirrors ``maxillo/views/patient_upload.py::bulk_upload_patients``, trimmed
    for a single modality: there is nothing to disambiguate by extension, so
    every file is just handed to ``save_ecg_to_dataset`` and reported per file
    (one bad file costs only its own row, the rest of the batch still runs).
    """
    namespace = "cardiology"
    current_project_id = request.session.get("current_project_id")
    current_project = None
    if current_project_id:
        current_project = Project.objects.filter(id=current_project_id).first()

    # Bulk ingestion creates patients wholesale and bypasses per-case review, so
    # it is administrators only (the single-patient upload stays open to annotators).
    if not current_project or not user_is_project_admin(request.user, current_project):
        message = "Bulk upload is restricted to project administrators."
        if wants_json(request):
            return JsonResponse({"ok": False, "error": message}, status=403)
        messages.error(request, message)
        return redirect_with_namespace(request, "patient_list")

    ecg_modality = current_project.modalities.filter(slug="ecg", is_active=True).first()
    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(project_id=current_project.id).order_by("name"),
        namespace,
    )

    if request.method == "GET":
        return render(request, "common/upload/bulk_upload.html", {
            "current_project": current_project,
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
        _bulk_upload_one_ecg(request, current_project, folder, ecg_modality, uploaded_file)
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
    except Exception as exc:  # noqa: BLE001 - reported per file (InvalidEcgFile or otherwise), batch continues
        detail = str(exc)
        logger.warning("Bulk ECG upload failed for %s: %s", filename, detail)
        return {"file": filename, "ok": False, "error": detail}

    log_activity(
        request.user, "cardiology", patient.patient_id,
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
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not (user_is_project_admin(request.user, patient.project) or user_can_write_patient_annotations(request.user, patient)):
        return JsonResponse({"error": "Permission denied"}, status=403)
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
def delete_patient(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    can_delete = bool(
        user_is_project_admin(request.user, patient.project)
        or (patient.folder and user_can_delete_single_patient(request.user, patient.folder, patient.project))
    )
    if not can_delete:
        return JsonResponse({"success": False, "error": "You do not have permission to delete this patient."}, status=403)
    patient.deleted = True
    patient.save(update_fields=["deleted"])
    return JsonResponse({"success": True, "message": "Patient deleted successfully"})


@login_required
@require_POST
def bulk_delete_patients(request):
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON payload"}, status=400)

    scan_ids = data.get("scan_ids", [])
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse({"success": False, "error": "scan_ids list is required"}, status=400)
    if not user_is_project_admin(request.user, "cardiology"):
        return JsonResponse({"success": False, "error": "You do not have permission to bulk delete patients."}, status=403)

    deleted_count = Patient.objects.filter(patient_id__in=scan_ids).update(deleted=True)
    if not deleted_count:
        return JsonResponse({"success": False, "error": "No valid patients found to delete"}, status=404)
    return JsonResponse({"success": True, "message": f"Successfully deleted {deleted_count} patients.", "deleted_count": deleted_count})


@login_required
def user_profile(request, username=None):
    if username is None:
        target_user = request.user
    else:
        if not request.user.profile.can_view_other_profiles():
            messages.error(request, "You do not have permission to view other user profiles.")
            return redirect_with_namespace(request, "user_profile")
        target_user = get_object_or_404(User, username=username)

    active_project_id = getattr(getattr(request.user, "profile", None), "project_id", None) or request.session.get("current_project_id")

    patients_uploaded = Patient.objects.filter(uploaded_by=target_user).order_by("-uploaded_at")
    if active_project_id:
        patients_uploaded = patients_uploaded.filter(project_id=active_project_id)

    classifications = Classification.objects.filter(annotator=target_user, classifier="manual").select_related("patient").order_by("-timestamp")
    if active_project_id:
        classifications = classifications.filter(patient__project_id=active_project_id)

    voice_captions = VoiceCaption.objects.filter(user=target_user).select_related("patient").order_by("-created_at")
    if active_project_id:
        voice_captions = voice_captions.filter(patient__project_id=active_project_id)

    target_profile = None
    if active_project_id:
        target_profile = ProjectAccess.objects.filter(user=target_user, project_id=active_project_id).first()

    context = {
        "target_user": target_user,
        "target_profile": target_profile,
        "is_own_profile": target_user == request.user,
        "is_viewing_other_profile": request.user.profile.can_view_other_profiles() and target_user != request.user,
        "total_patients_uploaded": patients_uploaded.count(),
        "total_classifications": classifications.count(),
        "unique_patients_annotated": classifications.values("patient").distinct().count(),
        "total_voice_captions": voice_captions.count(),
        "recent_uploads": patients_uploaded[:20],
        "recent_classifications": classifications[:20],
        "recent_voice_captions": voice_captions[:20],
    }
    if request.user.profile.can_view_other_profiles() and target_user == request.user:
        context["all_users"] = User.objects.order_by("username")
    return render_with_fallback(request, "user_profile", context)


@login_required
@require_POST
def create_folder(request):
    try:
        if not user_is_project_admin(request.user, "cardiology"):
            return JsonResponse({"error": "Permission denied"}, status=403)
        current_project_id = request.session.get("current_project_id")
        project = Project.objects.filter(id=current_project_id, is_active=True).first() if current_project_id else None
        if project is None:
            return JsonResponse({"error": "No project selected"}, status=400)
        data = _json.loads(request.body) if request.body else request.POST
        name = (data.get("name") or "").strip()
        if not name:
            return JsonResponse({"error": "Folder name is required"}, status=400)
        folder, created = Folder.objects.get_or_create(
            name=name, parent=None, project=project, defaults={"created_by": request.user},
        )
        return JsonResponse({"success": True, "folder": {"id": folder.id, "name": folder.name, "path": folder.name, "created": created}})
    except Exception as exc:
        logger.exception("Error creating cardiology folder")
        return JsonResponse({"error": str(exc)}, status=500)


@login_required
def folder_stats(request, folder_id):
    folder = get_object_or_404(Folder, id=folder_id)
    return JsonResponse({"success": True, "folder": {"id": folder.id, "name": folder.name}, "stats": {"patient_count": folder.patients.count()}})


@login_required
@require_POST
def rename_folder(request, folder_id):
    if not user_is_project_admin(request.user, "cardiology"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON payload"}, status=400)
    name = (data.get("name") or "").strip()
    if not name:
        return JsonResponse({"success": False, "error": "Folder name is required"}, status=400)
    folder = get_object_or_404(Folder, id=folder_id)
    folder.name = name
    folder.parent = None
    folder.save(update_fields=["name", "parent"])
    return JsonResponse({"success": True, "folder": {"id": folder.id, "name": folder.name}})


@login_required
@require_http_methods(["DELETE"])
def delete_folder(request, folder_id):
    from common.deletion import FolderNotEmpty, delete_folder as _delete_folder
    folder = get_object_or_404(Folder, id=folder_id)
    if not user_is_project_admin(request.user, folder.project):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        unfiled = _delete_folder(folder, force=request.GET.get("force") == "true")
    except FolderNotEmpty as exc:
        return JsonResponse({"success": False, "error": str(exc), "patient_count": exc.patient_count}, status=400)
    return JsonResponse({"success": True, "unfiled_patients": unfiled})


@login_required
@require_POST
def move_patients_to_folder(request):
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON payload"}, status=400)
    scan_ids = data.get("scan_ids", [])
    folder_id = data.get("folder_id")
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse({"success": False, "error": "scan_ids list is required"}, status=400)
    if not user_is_project_admin(request.user, "cardiology"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    folder = None
    if folder_id and folder_id not in ("root", "all"):
        folder = get_object_or_404(Folder, id=folder_id)
    patients = Patient.objects.filter(patient_id__in=scan_ids)
    updated = 0
    for patient in patients:
        patient.folder = folder
        if folder:
            patient.project = folder.project
        patient.save(update_fields=["folder", "project"])
        updated += 1
    return JsonResponse({"success": True, "updated": updated})


@login_required
@require_POST
def add_patient_tag(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not (user_is_project_admin(request.user, patient.project) or user_can_write_patient_annotations(request.user, patient)):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON payload"}, status=400)
    tag_name = (data.get("tag") or data.get("name") or "").strip()
    if not tag_name:
        return JsonResponse({"success": False, "error": "Tag name required"}, status=400)
    tag, _ = Tag.objects.get_or_create(name=tag_name)
    patient.tags.add(tag)
    return JsonResponse({"success": True, "tags": patient.tag_names()})


@login_required
@require_POST
def remove_patient_tag(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not (user_is_project_admin(request.user, patient.project) or user_can_write_patient_annotations(request.user, patient)):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON payload"}, status=400)
    tag_name = (data.get("tag") or data.get("name") or "").strip()
    if not tag_name:
        return JsonResponse({"success": False, "error": "Tag name required"}, status=400)
    tag = Tag.objects.filter(name=tag_name).first()
    if not tag:
        return JsonResponse({"success": False, "error": "Tag not found"}, status=404)
    patient.tags.remove(tag)
    return JsonResponse({"success": True, "tags": patient.tag_names()})


@login_required
@require_POST
def upload_text_caption(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not (user_is_project_admin(request.user, patient.project) or user_can_write_patient_annotations(request.user, patient)):
        return JsonResponse({"error": "Permission denied"}, status=403)
    if not project_allows_annotation(patient, "voice_caption"):
        return JsonResponse({"error": "Captions are disabled for this project"}, status=403)
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    text = data.get("text") or data.get("caption") or ""
    if not text.strip():
        return JsonResponse({"error": "Caption text is required"}, status=400)
    caption = patient.voice_captions.create(
        # Cardiology has exactly one modality, unlike maxillo/brain where this
        # field disambiguates which scan a note is about -- set it explicitly
        # so the "reports.captions" export artifact's modality filter (see
        # common.export_processing.ExportProcessor._collect_captions) actually
        # picks the note up instead of silently excluding it.
        user=request.user, duration=0, text_caption=text.strip(),
        original_text_caption=text.strip(), processing_status="completed",
        modality="ecg",
    )
    return JsonResponse({
        "success": True,
        "caption": {
            "id": caption.id,
            "user_username": caption.user.username,
            "display_duration": "Text",
            "quality_color": "success",
            "created_at": caption.created_at.strftime("%b %d, %H:%M"),
            "audio_url": None,
            "is_processed": True,
            "text_caption": caption.text_caption,
            "is_text_caption": True,
        },
    })


@login_required
@require_http_methods(["DELETE"])
def delete_voice_caption(request, patient_id, caption_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    caption = get_object_or_404(patient.voice_captions, id=caption_id)
    is_owner = caption.user_id == request.user.id
    is_admin = user_is_project_admin(request.user, caption.patient.project)
    if not is_owner and not is_admin:
        return JsonResponse({"error": "You cannot delete voice captions created by other users.", "code": "not_owner"}, status=403)
    if is_admin and not is_owner:
        data = _json.loads(request.body) if request.body else {}
        if not data.get("admin_confirmed"):
            return JsonResponse({
                "error": "Admin confirmation required",
                "code": "admin_confirmation_required",
                "message": f"You are about to delete a caption created by {caption.user.username}. Please confirm this action.",
            }, status=403)
    caption.delete()
    return JsonResponse({"success": True})


@login_required
@require_POST
def edit_voice_caption_transcription(request, patient_id, caption_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    caption = get_object_or_404(patient.voice_captions, id=caption_id)
    if not user_can_edit_caption(request.user, caption):
        return JsonResponse({"error": "You do not have permission to edit this caption.", "code": "permission_denied"}, status=403)
    try:
        data = _json.loads(request.body) if request.body else {}
        action = data.get("action")
        if action == "edit":
            new_text = (data.get("text") or "").strip()
            if not new_text:
                return JsonResponse({"error": "Text cannot be empty"}, status=400)
            caption.edit_transcription(new_text, request.user)
        elif action == "revert":
            caption.revert_to_original(request.user)
        else:
            return JsonResponse({"error": 'Invalid action. Use "edit" or "revert"'}, status=400)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({
        "success": True,
        "caption": {"id": caption.id, "text_caption": caption.text_caption, "is_edited": caption.is_edited, "edit_history": caption.edit_history},
    })


@login_required
def update_voice_caption_modality(request, patient_id, caption_id):
    return JsonResponse({"error": "Modality is not used for cardiology captions."}, status=400)


@login_required
@require_POST
def classification_update(request, patient_id):
    """Set the AF/NSR/Other/NI call for one patient's ECG."""
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not (user_is_project_admin(request.user, patient.project) or user_can_write_patient_annotations(request.user, patient)):
        return JsonResponse({"error": "Permission denied"}, status=403)
    if not project_allows_annotation(patient, "ecg_classification"):
        return JsonResponse({"error": "ECG classification is disabled for this project"}, status=403)
    try:
        data = _json.loads(request.body)
    except _json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    value = data.get("value")
    valid_values = [choice for choice, _label in Classification.VALUE_CHOICES]
    if value not in valid_values:
        return JsonResponse({"error": f"value must be one of {valid_values}"}, status=400)
    classification, _created = Classification.objects.update_or_create(
        patient=patient, classifier="manual", defaults={"value": value, "annotator": request.user},
    )
    return JsonResponse({"success": True, "value": classification.value, "display_value": classification.get_value_display()})


@login_required
def serve_ecg_file(request, patient_id, file_id):
    """Stream a patient's raw ECG JSON for the client-side plotter to fetch."""
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not user_has_project_access(request.user, patient.project) and not user_is_project_admin(request.user, patient.project):
        return JsonResponse({"error": "Permission denied"}, status=403)
    file_obj = get_object_or_404(FileRegistry, id=file_id, domain="cardiology", cardiology_patient=patient)
    allowed, error, status = authorize_file_read(request.user, file_obj)
    if not allowed:
        return JsonResponse({"error": error}, status=status)
    filename = (file_obj.metadata or {}).get("original_filename") or os.path.basename(file_obj.file_path)
    return streaming_response(path_or_key=file_obj.file_path, content_type="application/json", filename=filename)


# ─── Browser-generated ECG plot (mirrors maxillo's browser panoramic) ────────
#
# The clinical-grid PNG is reconstructed client-side (static/js/cardiology/ecg_plot.js)
# the first time anyone opens the patient, then POSTed back here so it becomes a
# real export artifact instead of something that only ever exists in one browser
# tab. `ecg_warmup`/`ecg_warmup_pending` let an admin force that first-open pass
# for a whole folder without visiting each patient by hand -- same mechanism as
# maxillo/views/panoramic_warmup.py, just without panoramic's arch/geometry/
# annotation-lock machinery, which has no ECG equivalent.

ECG_PLOT_ALGORITHM_VERSION = "ecg-plot-canvas-v1"
ECG_PLOT_MAX_REQUEST_BYTES = 5 * 1024 * 1024
ECG_PLOT_MAX_PIXELS = 4000 * 4000
ECG_WARMUP_BATCH_LIMIT = 200


def has_browser_ecg_plot(patient):
    return FileRegistry.objects.filter(
        domain="cardiology", cardiology_patient=patient, file_type="ecg_processed",
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
    project = _current_export_project(request)
    if project is None or not user_is_project_admin(request.user, project):
        messages.error(request, "Only project administrators can run this.")
        return redirect_with_namespace(request, "patient_list")
    folders = filter_folders_for_user(
        request.user, Folder.objects.filter(project=project).order_by("name"), "cardiology",
    )
    return render(request, "cardiology/ecg_warmup.html", {
        "project": project, "folders": folders, "batch_limit": ECG_WARMUP_BATCH_LIMIT, "ns": "cardiology",
    })


@login_required
@require_http_methods(["GET"])
def ecg_warmup_pending(request):
    project = _current_export_project(request)
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


@login_required
@require_POST
def save_browser_ecg_plot(request, patient_id):
    """The browser's one-time POST of the clinical-grid PNG it just rendered."""
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not (user_is_project_admin(request.user, patient.project) or user_can_write_patient_annotations(request.user, patient)):
        return JsonResponse({"error": "Permission denied"}, status=403)

    content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    if content_length > ECG_PLOT_MAX_REQUEST_BYTES:
        return JsonResponse({"error": "Payload too large"}, status=413)

    raw_ecg = patient.get_ecg_raw_file()
    if raw_ecg is None:
        return JsonResponse({"error": "This patient has no ECG recording to plot."}, status=409)

    png_file = request.FILES.get("plot_png")
    generation_uuid = (request.POST.get("generation_uuid") or "").strip()
    if not png_file or not generation_uuid:
        return JsonResponse({"error": "plot_png and generation_uuid are required"}, status=400)

    # Re-decode/re-encode with Pillow rather than trust the uploaded bytes verbatim --
    # this PNG goes into every export ZIP from here on, so a corrupt or disguised
    # file should fail here, not surface downstream. Mirrors save_browser_panoramic's
    # own sanitization (maxillo/views/patient_data.py::_sanitize_browser_png).
    from PIL import Image, UnidentifiedImageError
    import hashlib
    import io

    try:
        image = Image.open(png_file)
        image.verify()
        png_file.seek(0)
        image = Image.open(png_file)
        if image.width * image.height > ECG_PLOT_MAX_PIXELS:
            return JsonResponse({"error": "Image is too large"}, status=400)
        if image.mode not in ("RGB", "RGBA", "L", "LA"):
            image = image.convert("RGBA")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        png_bytes = buffer.getvalue()
    except (UnidentifiedImageError, OSError, ValueError):
        return JsonResponse({"error": "plot_png is not a valid PNG"}, status=400)

    with transaction.atomic():
        # Row lock on the patient serializes concurrent saves the same way
        # save_browser_panoramic locks the patient row before its idempotency check.
        Patient.objects.select_for_update().get(pk=patient.pk)
        existing = (
            FileRegistry.objects.filter(domain="cardiology", cardiology_patient=patient, file_type="ecg_processed")
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            existing_meta = existing.metadata or {}
            if existing_meta.get("generation_uuid") == generation_uuid:
                return JsonResponse({"success": True, "idempotent": True, "outcome": "existing"})
            if existing_meta.get("algorithm_version") == ECG_PLOT_ALGORITHM_VERSION:
                return JsonResponse({"success": True, "idempotent": True, "outcome": "existing"})

        from common.uploads import processed_key_prefix_for

        key = f"{processed_key_prefix_for(patient, 'ecg')}/ecg_plot_{patient.patient_id}_{generation_uuid}.png"
        storage = get_object_storage()
        storage.upload_fileobj(io.BytesIO(png_bytes), key=key, content_type="image/png")

        modality = patient.modalities.filter(slug="ecg").first()
        FileRegistry.objects.create(
            file_type="ecg_processed",
            file_path=key,
            file_size=len(png_bytes),
            file_hash=hashlib.sha256(png_bytes).hexdigest(),
            modality=modality,
            domain="cardiology",
            cardiology_patient=patient,
            metadata={
                "generated_from": "browser_ecg_plot",
                "algorithm_version": ECG_PLOT_ALGORITHM_VERSION,
                "generation_uuid": generation_uuid,
                "source_file_id": raw_ecg.id,
            },
        )

    return JsonResponse({"success": True, "idempotent": False, "outcome": "created"})


# ─── Export ─────────────────────────────────────────────────────────────────

def _cardiology_shared_export_availability(share_token):
    export = Export.objects.filter(share_token=share_token).first()
    if not export:
        return None, False, "invalid"
    if export.share_mode == "private":
        return export, False, "private"
    if is_share_expired(export):
        return export, False, "expired"
    if export.status != "completed":
        return export, False, "not_completed"
    if not export.file_path or not artifact_exists(export.file_path):
        return export, False, "missing_file"
    return export, True, ""


def _current_export_project(request):
    project_id = request.session.get("current_project_id")
    if not project_id:
        return None
    return Project.objects.filter(id=project_id).prefetch_related("modalities", "annotation_methods", "disabled_steps").first()


@login_required
def export_list(request):
    exports = Export.objects.filter(user=request.user).order_by("-created_at")
    exports_with_sizes = [
        {"export": export, "size_display": format_file_size(export.file_size) if export.file_size else None}
        for export in exports
    ]
    page_obj = Paginator(exports_with_sizes, 50).get_page(request.GET.get("page"))
    return render(request, "maxillo/export_list.html", {"exports": page_obj, "page_obj": page_obj, "ns": "cardiology"})


@login_required
def export_new(request):
    project = _current_export_project(request)
    if project is None:
        messages.error(request, "Select a project before creating an export.")
        return redirect("cardiology:patient_list")

    if request.method == "POST":
        folder_ids = [int(fid) for fid in request.POST.getlist("folder_ids")]
        artifact_keys = request.POST.getlist("artifacts")
        filters = export_catalog.filters_from_form(request.POST)

        if not folder_ids:
            messages.error(request, "Please select at least one folder.")
            return redirect("cardiology:export_new")
        if Folder.objects.filter(id__in=folder_ids, project=project).count() != len(set(folder_ids)):
            messages.error(request, "Select folders from the current project only.")
            return redirect("cardiology:export_new")

        allowed_keys = export_ui.allowed_artifact_keys("cardiology", project)
        artifact_keys = [key for key in artifact_keys if key in allowed_keys]
        if not artifact_keys:
            messages.error(request, "Please select at least one artifact to export.")
            return redirect("cardiology:export_new")

        artifacts = export_catalog.resolve_artifacts("cardiology", artifact_keys)
        query_params = {
            "domain": "cardiology",
            "project_id": project.id,
            "folder_ids": folder_ids,
            "artifacts": artifact_keys,
            "filters": filters,
            "modality_slugs": sorted(export_catalog.modality_slugs_for(artifacts)),
        }

        summary_parts = [f"{len(folder_ids)} folder{'s' if len(folder_ids) != 1 else ''}"]
        summary_parts.append(", ".join(a.label for a in artifacts) or "nothing")
        described = export_catalog.describe_filters(
            "cardiology", project, [m.slug for m in export_ui.project_modalities(project)], filters
        )
        if described:
            summary_parts.append(", ".join(described))

        export = Export.objects.create(
            user=request.user, status="pending", query_params=query_params,
            query_summary=", ".join(summary_parts),
        )
        start_export_processing(export.id, "cardiology")
        messages.success(request, f"Export #{export.id} created and processing started.")
        return redirect("cardiology:export_list")

    folders = export_ui.folder_tree(
        filter_folders_for_user(request.user, Folder.objects.filter(project=project).order_by("name"), "cardiology"),
        Patient, "cardiology",
    )
    visible_folder_ids = [entry["folder"].id for entry in folders]
    patients_in_scope = Patient.objects.filter(folder_id__in=visible_folder_ids)
    modalities = export_ui.project_modalities(project)

    # ECG plots are reconstructed in the browser (see save_browser_ecg_plot), so
    # already-uploaded patients may have none to export yet. Same soft nudge
    # maxillo's export page gives for browser panoramics: it doesn't block, just
    # points admins at the batch page.
    warmup_url = reverse("cardiology:ecg_warmup") if user_is_project_admin(request.user, project) else None

    return render(request, "maxillo/export_new.html", {
        "project": project,
        "folders": folders,
        "modalities": modalities,
        "warmup_url": warmup_url,
        "warmup_message": (
            "ECG plots are reconstructed in the browser, so patients uploaded "
            "before anyone opened them may not have one yet."
        ),
        "warmup_cta": "Generate the missing ECG plots",
        "artifact_groups": export_ui.artifact_groups("cardiology", project, patients_in_scope, patient_fk="cardiology_patient"),
        "filter_groups": export_ui.grouped_filters("cardiology", project, [m.slug for m in modalities]),
        "ns": "cardiology",
    })


@login_required
def export_preview(request):
    try:
        data = (_json.loads(request.body) if request.body else {}) if request.method == "POST" else request.GET
        folder_ids = data.get("folder_ids", [])
        if isinstance(folder_ids, str):
            folder_ids = [fid for fid in folder_ids.split(",") if fid]
        folder_ids = [int(fid) for fid in folder_ids if str(fid).strip()]

        artifact_keys = data.get("artifacts", [])
        if isinstance(artifact_keys, str):
            artifact_keys = [key for key in artifact_keys.split(",") if key]

        query_params = {
            "domain": "cardiology", "folder_ids": folder_ids,
            "artifacts": list(artifact_keys), "filters": data.get("filters", {}),
        }
        proc = ExportProcessor(Export(user=request.user, query_params=query_params), domain="cardiology")
        patients = proc.query_patients()
        patient_count = patients.count()
        if patient_count:
            files, total_size = proc.collect_files(patients)
            file_count = len(files)
        else:
            file_count, total_size = 0, 0

        return JsonResponse({
            "success": True, "patient_count": patient_count, "folder_count": len(folder_ids),
            "modality_count": len(proc.modality_slugs), "artifact_count": len(proc.artifacts),
            "file_count": file_count, "estimated_size": format_file_size(total_size), "estimated_size_bytes": total_size,
        })
    except Exception as exc:
        logger.error(f"Error in cardiology export_preview: {exc}", exc_info=True)
        return JsonResponse({"success": False, "error": str(exc)}, status=500)


@login_required
def export_status(request, export_id):
    export = get_object_or_404(Export, id=export_id)
    if export.user != request.user and not user_is_project_admin(request.user, "cardiology"):
        return JsonResponse({"error": "Permission denied"}, status=403)
    export = _recover_stuck_export(export)
    data = {"id": export.id, "status": export.status, "query_summary": export.query_summary}
    if export.status == "completed":
        data["file_size"] = export.file_size
        data["file_size_human"] = format_file_size(export.file_size)
        data["patient_count"] = export.patient_count
        if export.completed_at:
            data["completed_at"] = export.completed_at.isoformat()
    if export.status == "failed":
        data["error_message"] = export.error_message
    if export.status == "processing":
        if export.started_at:
            data["started_at"] = export.started_at.isoformat()
        if export.patient_count:
            data["patient_count"] = export.patient_count
        if export.progress_message:
            data["progress_message"] = export.progress_message
        if export.progress_percent is not None:
            data["progress_percent"] = export.progress_percent
    return JsonResponse(data)


@login_required
def export_download(request, export_id):
    export = get_object_or_404(Export, id=export_id)
    if export.user != request.user and not user_is_project_admin(request.user, "cardiology"):
        messages.error(request, "You do not have permission to download this export.")
        return redirect("cardiology:export_list")
    if export.status != "completed":
        messages.error(request, "Export is not yet completed.")
        return redirect("cardiology:export_list")
    if not export.file_path or not artifact_exists(export.file_path):
        messages.error(request, "Export file not found.")
        export.mark_failed("Export file not found in storage")
        return redirect("cardiology:export_list")
    filename = os.path.basename((export.file_path or "").rstrip("/")) or f"export_{export.id}.zip"
    return streaming_response(path_or_key=export.file_path, content_type="application/zip", filename=filename, as_attachment=True)


@login_required
@require_POST
def export_share_update(request, export_id):
    export = get_object_or_404(Export, id=export_id)
    if export.user != request.user and not user_is_project_admin(request.user, "cardiology"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if export.status != "completed":
        return JsonResponse({"success": False, "error": "Only completed exports can be shared"}, status=400)
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid JSON payload"}, status=400)

    share_mode = (data.get("share_mode") or "").strip()
    if share_mode not in ("private", "authenticated", "public"):
        return JsonResponse({"success": False, "error": "Invalid share mode"}, status=400)

    regenerate_raw = data.get("regenerate", False)
    regenerate = regenerate_raw if isinstance(regenerate_raw, bool) else str(regenerate_raw).lower() in ("1", "true", "yes")

    export.share_mode = share_mode
    if share_mode == "private":
        export.share_token = None
        export.shared_at = None
        export.expires_at = None
        export.save(update_fields=["share_mode", "share_token", "shared_at", "expires_at"])
        return JsonResponse({"success": True, "share_mode": export.share_mode, "share_url": None, "expires_at": None})

    expires_at, expiry_error = resolve_share_expiry(
        data.get("expires_in_days"), current=export.expires_at,
        can_set_never=request.user.is_staff or user_is_project_admin(request.user, "cardiology"),
    )
    if expiry_error:
        return JsonResponse({"success": False, "error": expiry_error}, status=400)

    if regenerate or not export.share_token:
        export.ensure_share_token(force_new=regenerate)
    export.shared_at = timezone.now()
    export.expires_at = expires_at
    export.save(update_fields=["share_mode", "shared_at", "expires_at"])

    return JsonResponse({
        "success": True, "share_mode": export.share_mode,
        "share_url": _build_shared_download_url(request, export.share_token),
        "expires_at": export.expires_at.isoformat() if export.expires_at else None,
    })


@require_http_methods(["GET"])
def export_shared_landing(request, share_token):
    export, is_available, reason = _cardiology_shared_export_availability(share_token)
    if export and export.share_mode == "authenticated" and not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    return render(request, "maxillo/export_shared_landing.html", {
        "ns": "cardiology", "export": export, "is_available": is_available,
        "is_expired": reason == "expired", "share_token": share_token,
        "file_size_human": format_file_size(export.file_size) if export and export.file_size else None,
    }, status=410 if reason == "expired" else 200)


@require_http_methods(["GET"])
def export_shared_download(request, share_token):
    export, is_available, reason = _cardiology_shared_export_availability(share_token)
    if reason == "expired":
        return HttpResponseGone("This share link has expired.")
    if not export or not is_available:
        raise Http404("Export is not available.")
    if export.share_mode == "authenticated" and not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    filename = os.path.basename((export.file_path or "").rstrip("/")) or f"export_{export.id}.zip"
    return streaming_response(path_or_key=export.file_path, content_type="application/zip", filename=filename, as_attachment=True)


@login_required
@require_POST
def export_delete(request, export_id):
    export = get_object_or_404(Export, id=export_id)
    if export.user != request.user and not user_is_project_admin(request.user, "cardiology"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    file_path = export.file_path
    deleted_count, _ = Export.objects.filter(id=export_id).delete()
    if not deleted_count:
        return JsonResponse({"success": False, "error": "Export not found or already deleted."}, status=404)
    if file_path:
        try:
            get_object_storage().delete(file_path)
        except Exception as exc:
            logger.warning(f"Could not delete export file {file_path}: {exc}")
    return JsonResponse({"success": True})


@login_required
@require_POST
def export_stop(request, export_id):
    export = get_object_or_404(Export, id=export_id)
    if export.user != request.user and not user_is_project_admin(request.user, "cardiology"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if export.status not in {"processing", "pending"}:
        return JsonResponse({"success": False, "error": f"Export is not running (status: {export.status})."}, status=409)

    killed_pids = _kill_export_processes(export.id)
    deleted_keys = []
    warnings = []
    storage = get_object_storage()

    if export.file_path:
        try:
            storage.delete(export.file_path)
            deleted_keys.append(export.file_path)
        except Exception as exc:
            warnings.append(f"Could not delete {export.file_path}: {exc}")

    prefix = f"exports/export_{export.id}_"
    try:
        for key in storage.list_keys(prefix):
            if not key.startswith(prefix) or not key.endswith(".zip"):
                continue
            try:
                storage.delete(key)
                deleted_keys.append(key)
            except Exception as exc:
                warnings.append(f"Could not delete {key}: {exc}")
    except Exception as exc:
        warnings.append(f"Could not list keys for prefix {prefix}: {exc}")

    who = getattr(request.user, "username", "unknown")
    stopped_at = timezone.now().strftime("%Y-%m-%d %H:%M:%S %Z")
    message = f"Stopped manually by {who} at {stopped_at}."
    if killed_pids:
        message += f" Killed worker PID(s): {', '.join(str(p) for p in killed_pids)}."
    if deleted_keys:
        message += f" Deleted {len(set(deleted_keys))} ZIP object(s)."
    export.mark_failed(message)

    return JsonResponse({
        "success": True, "killed_pids": killed_pids, "deleted_keys": sorted(set(deleted_keys)),
        "warnings": warnings, "status": "failed", "error_message": message,
    })
