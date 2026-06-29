"""Brain views."""

import json as _json
import logging
import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST
from django.http import JsonResponse, Http404
from django.utils import timezone
from django.contrib.auth.views import redirect_to_login

from common.file_access import exists as artifact_exists, streaming_response
from common.models import FileRegistry, Job, Modality, Project, ProjectAccess
from common.object_storage import get_object_storage
from common.permissions import (
    filter_folders_for_user,
    filter_patients_for_user,
    user_can_delete_single_patient,
    user_can_edit_caption,
    user_can_read_folder,
    user_can_write_annotations,
    user_is_project_admin,
)

from maxillo.utils.export_processor import ExportProcessor, start_export_processing
from maxillo.views.export import (
    _build_shared_download_url,
    _coerce_bool,
    _kill_export_processes,
    _recover_stuck_export,
    _resolve_content_selection,
    format_file_size,
)
from .export_config import install_brain_export_mappings
from .file_utils import save_brain_modality_file
from .forms import PatientForm, PatientManagementForm, PatientUploadForm
from .helpers import redirect_with_namespace, render_with_fallback
from .models import Export, Folder, FolderAccess, Patient, Tag, UserPreference


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
        return render(request, "common/landing.html", {
            "projects": projects.order_by("name"),
            "current_project_id": current_project_id,
            "current_project_name": current_project_name,
            "continue_url": "/brain/" if current_project_name else None,
        })
    return render(request, "common/landing.html")


@login_required
def select_project(request, project_id):
    project = get_object_or_404(Project, id=project_id, is_active=True)
    if not user_is_project_admin(request.user, project):
        has_access = ProjectAccess.objects.filter(user=request.user, project=project).exists()
        if not has_access:
            messages.error(request, f"You don't have access to the {project.name} project.")
            return redirect("home")
    request.session["current_project_id"] = project.id
    messages.success(request, f"Project set to {project.name}")
    return redirect("brain:patient_list")


@login_required
def patient_detail(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    can_view = bool(any(user_can_read_folder(request.user, f, request) for f in patient.folders.all()))
    if user_is_project_admin(request.user, "brain"):
        can_view = True
    if not can_view:
        messages.error(request, "You do not have permission to view this scan.")
        return redirect("brain:patient_list")

    management_form = PatientManagementForm(instance=patient, user=request.user)

    can_modify = bool(any(user_can_write_annotations(request.user, f, request) for f in patient.folders.all()))
    if user_is_project_admin(request.user, "brain"):
        can_modify = True

    if request.method == "POST" and can_modify:
        action = request.POST.get("action")
        if action == "update_management":
            management_form = PatientManagementForm(request.POST, instance=patient, user=request.user)
            if management_form.is_valid():
                management_form.save()
                messages.success(request, "Scan settings updated successfully!")
                return redirect("brain:patient_detail", patient_id=patient_id)

    patient_modalities = []
    for modality in patient.modalities.all().order_by("name"):
        patient_modalities.append({
            "slug": modality.slug,
            "name": modality.name,
            "label": modality.label or "",
            "subtypes": list(modality.subtypes or []),
        })

    modality_files = {}
    segmentation_file = None
    for item in patient_modalities:
        modality = Modality.objects.filter(slug=item["slug"]).first()
        if not modality:
            continue
        file_obj = patient.files.filter(modality=modality).order_by("-created_at").first()
        if not file_obj:
            continue
        payload = {"id": file_obj.id, "file_type": file_obj.file_type}
        if item["slug"] == "braintumor-mri-seg":
            segmentation_file = payload
        else:
            modality_files[item["slug"]] = payload

    patient_files = {"raw": [], "processed": [], "other": []}
    for file_obj in patient.files.all().order_by("-created_at"):
        file_data = {
            "id": file_obj.id,
            "file_type": file_obj.file_type,
            "file_path": file_obj.file_path,
            "file_size": file_obj.file_size,
            "created_at": file_obj.created_at,
            "filename": os.path.basename(file_obj.file_path) if file_obj.file_path else "Unknown",
            "original_filename": file_obj.metadata.get("original_filename", "") if file_obj.metadata else "",
            "file_size_mb": f"{file_obj.file_size / (1024 * 1024):.2f}" if file_obj.file_size else "0.00",
            "modality_name": file_obj.modality.name if file_obj.modality else "",
        }
        if "_raw" in file_obj.file_type or file_obj.file_type == "rgb_image":
            patient_files["raw"].append(file_data)
        elif "_processed" in file_obj.file_type or file_obj.file_type == "bite_classification":
            patient_files["processed"].append(file_data)
        else:
            patient_files["other"].append(file_data)

    voice_captions = patient.voice_captions.all()
    is_admin_user = user_is_project_admin(request.user, "brain")
    for caption in voice_captions:
        caption.can_view_content = bool(is_admin_user or caption.user_id == request.user.id)
        caption.can_edit_content = bool(is_admin_user or caption.user_id == request.user.id)
        caption.is_ghost = not caption.can_view_content

    pref = UserPreference.objects.filter(user=request.user).first()
    allowed_modalities = list(Modality.objects.filter(projects__id=request.session.get("current_project_id"), is_active=True))
    if not allowed_modalities:
        allowed_modalities = list(Modality.objects.filter(is_active=True))

    context = {
        "patient": patient,
        "user_profile": request.user.profile,
        "management_form": management_form,
        "has_cbct": False,
        "has_uploaded_panoramic": False,
        "has_intraoral_modality": False,
        "can_modify_segmentation": can_modify,
        "can_create_caption": can_modify,
        "patient_modalities": patient_modalities,
        "default_modality_slug": next((m["slug"] for m in patient_modalities if m["slug"] != "braintumor-mri-seg"), None),
        "patient_modalities_json": _json.dumps(patient_modalities),
        "default_modality_json": _json.dumps(next((m["slug"] for m in patient_modalities if m["slug"] != "braintumor-mri-seg"), None)),
        "patient_files": patient_files,
        "voice_captions": voice_captions,
        "is_admin_user": is_admin_user,
        "modality_files": modality_files,
        "modality_files_json": _json.dumps(modality_files),
        "segmentation_file": segmentation_file,
        "segmentation_file_json": _json.dumps(segmentation_file),
        "allowed_modalities": allowed_modalities,
        "allowed_modality_slugs": [m.slug for m in allowed_modalities],
        "report_language": pref.report_language if pref else "it",
    }
    return render_with_fallback(request, "patient_detail", context)


@login_required
def patient_list(request):
    patients = Patient.objects.select_related("dataset", "uploaded_by").prefetch_related(
        "voice_captions",
        "voice_captions__user",
        "tags",
        "modalities",
        "files",
        "files__modality",
        "jobs",
    )
    current_project_id = request.session.get("current_project_id")
    if current_project_id and any(field.name == "project" for field in Patient._meta.fields):
        patients = patients.filter(project_id=current_project_id)
    patients = filter_patients_for_user(request.user, patients, "brain")
    patients_for_folder_counts = patients

    search_query = request.GET.get("search", "").strip()
    if search_query:
        patients = patients.filter(Q(name__icontains=search_query) | Q(patient_id__icontains=search_query))

    folder_id = request.GET.get("folder")
    if folder_id and folder_id != "all":
        if folder_id == "root":
            patients = patients.filter(folders__isnull=True)
        else:
            try:
                patients = patients.filter(folders__id=int(folder_id)).distinct()
            except ValueError:
                pass

    tags_selected = request.GET.getlist("tags")
    if tags_selected:
        patients = patients.filter(tags__name__in=tags_selected).distinct()

    patients = patients.order_by("-uploaded_at")
    allowed_modalities = []
    if current_project_id:
        project = Project.objects.filter(id=current_project_id).prefetch_related("modalities").first()
        if project:
            allowed_modalities = list(project.modalities.filter(is_active=True))

    patients_with_status = []
    is_admin = user_is_project_admin(request.user, "brain")
    for patient in patients:
        voice_captions = list(patient.voice_captions.all())
        patient_files = list(patient.files.all())
        patient_jobs = list(patient.jobs.all()) if hasattr(patient, "jobs") else []
        files_by_modality = {}
        for file_obj in patient_files:
            if file_obj.modality and file_obj.modality.slug:
                files_by_modality.setdefault(file_obj.modality.slug, []).append(file_obj)
        jobs_by_modality = {}
        for job in patient_jobs:
            jobs_by_modality.setdefault(job.modality_slug, []).append(job)
        modality_status_list = []
        for modality in allowed_modalities:
            slug = modality.slug or ""
            if slug in {"rawzip", "voice"}:
                continue
            jobs = jobs_by_modality.get(slug, [])
            status = "absent"
            if any(job.status == "failed" for job in jobs):
                status = "failed"
            elif any(job.status == "processing" for job in jobs):
                status = "processing"
            elif any(job.status in ["pending", "retrying"] for job in jobs):
                status = "pending"
            elif files_by_modality.get(slug):
                status = "processed"
            modality_status_list.append({
                "slug": slug,
                "name": modality.name,
                "icon": modality.icon or "",
                "label": modality.label or "",
                "status": status,
            })
        patients_with_status.append({
            "patient": patient,
            "voice_caption_processing": any(vc.processing_status in ["pending", "processing"] for vc in voice_captions),
            "voice_caption_processed": bool(voice_captions) and all(vc.processing_status == "completed" for vc in voice_captions),
            "voice_caption_count": len(voice_captions),
            "voice_annotators": list({vc.user.username for vc in voice_captions}),
            "tags": patient.tag_names(),
            "folder": patient.folders.first(),
            "available_modalities": [m.slug for m in patient.modalities.all()],
            "modality_statuses": {item["slug"]: item["status"] for item in modality_status_list},
            "modality_status_list": modality_status_list,
            "can_delete": bool(is_admin or any(user_can_delete_single_patient(request.user, f, request) for f in patient.folders.all())),
        })

    per_page = int(request.GET.get("per_page", 20))
    page_obj = Paginator(patients_with_status, per_page).get_page(request.GET.get("page"))
    folders = filter_folders_for_user(request.user, Folder.objects.filter(parent__isnull=True).order_by("name"), "brain")
    context = {
        "page_obj": page_obj,
        "current_project_id": current_project_id,
        "search_query": search_query,
        "folder_id": folder_id or "all",
        "selected_tags": tags_selected,
        "folders": [{"folder": folder, "patient_count": patients_for_folder_counts.filter(folders=folder).count()} for folder in folders],
        "all_tags": Tag.objects.all().order_by("name"),
        "per_page": per_page,
        "user_profile": request.user.profile,
        "is_admin_user": is_admin,
        "allowed_modalities": allowed_modalities,
        "status_filters": {},
        "modality_filter_specs": [
            {"slug": m.slug, "name": m.name, "icon": m.icon or "", "label": m.label or "", "value": ""}
            for m in allowed_modalities
            if m.slug != "rawzip"
        ],
    }
    return render_with_fallback(request, "patient_list", context)


@login_required
def upload_patient(request):
    user_profile = request.user.profile
    namespace = "brain"

    if not request.user.profile:
        messages.error(request, "You do not have permission to upload scans.")
        return redirect_with_namespace(request, "patient_list")

    if not user_profile.can_upload_scans():
        messages.error(request, "You do not have permission to upload scans.")
        return redirect_with_namespace(request, "patient_list")

    if request.method == "POST":
        patient_upload_form = PatientUploadForm(request.POST, request.FILES, user=request.user)
        patient_form = PatientForm()

        if patient_upload_form.is_valid():
            patient = patient_upload_form.save(commit=False)
            patient.uploaded_by = request.user

            folder = patient_upload_form.cleaned_data.get("folder")
            if folder:
                allowed_folder_ids = set(
                    filter_folders_for_user(
                        request.user,
                        Folder.objects.filter(parent__isnull=True).only("id"),
                        namespace,
                    ).values_list("id", flat=True)
                )
                if folder.id not in allowed_folder_ids:
                    messages.error(request, "You do not have permission to upload to the selected folder.")
                    allowed_folders = filter_folders_for_user(
                        request.user,
                        Folder.objects.filter(parent__isnull=True).order_by("name"),
                        namespace,
                    )
                    return render(request, "common/upload/upload.html", {
                        "patient_form": patient_form,
                        "patient_upload_form": patient_upload_form,
                        "folders": allowed_folders,
                    })

            patient.save()
            patient_upload_form.instance = patient
            patient_upload_form.save(commit=True)

            if folder:
                patient.folders.set([folder])

            uploaded_modalities = []
            processing_job_ids = []
            brain_modalities = {
                "braintumor-mri-t1": "Brain MRI T1",
                "braintumor-mri-t2": "Brain MRI T2",
                "braintumor-mri-flair": "Brain MRI FLAIR",
                "braintumor-mri-t1c": "Brain MRI T1c",
                "braintumor-mri-seg": "Brain MRI Segmentation",
            }

            for slug, display_name in brain_modalities.items():
                file_obj = request.FILES.get(slug)
                if not file_obj:
                    continue
                try:
                    modality = Modality.objects.get(slug=slug)
                    patient.modalities.add(modality)

                    file_registry, job = save_brain_modality_file(patient, slug, file_obj)
                    if file_registry:
                        uploaded_modalities.append(display_name)
                        if job:
                            processing_job_ids.append(job.id)
                except Exception as exc:
                    logger.exception("Error saving %s", display_name)
                    messages.error(request, f"Error saving {display_name}: {exc}")

            if uploaded_modalities:
                unique_modalities = list(dict.fromkeys(uploaded_modalities))
                summary_message = (
                    f"Patient uploaded successfully with {len(unique_modalities)} modality(s): "
                    f"{', '.join(unique_modalities)}."
                )
                if processing_job_ids:
                    summary_message += f" Processing jobs: #{', #'.join(str(job_id) for job_id in processing_job_ids)}."
                messages.success(request, summary_message)
            else:
                messages.success(request, "Patient uploaded successfully!")

            return redirect_with_namespace(request, "patient_list")
    else:
        patient_form = PatientForm()
        patient_upload_form = PatientUploadForm(user=request.user)

    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(parent__isnull=True).order_by("name"),
        namespace,
    )

    allowed_modalities = []
    current_project_id = request.session.get("current_project_id")
    if current_project_id:
        try:
            project = Project.objects.prefetch_related("modalities").get(id=current_project_id)
            allowed_modalities = list(project.modalities.filter(is_active=True))
        except Project.DoesNotExist:
            pass

    return render(request, "common/upload/upload.html", {
        "patient_form": patient_form,
        "patient_upload_form": patient_upload_form,
        "folders": folders,
        "allowed_modalities": allowed_modalities,
    })


def _with_brain_export_mappings(view_func):
    def wrapped(request, *args, **kwargs):
        install_brain_export_mappings()
        return view_func(request, *args, **kwargs)

    return wrapped


@login_required
@require_POST
def update_patient_name(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not user_is_project_admin(request.user, "brain") and not (
        any(user_can_write_annotations(request.user, f, request) for f in patient.folders.all())
    ):
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
        user_is_project_admin(request.user, "brain")
        or any(user_can_delete_single_patient(request.user, f, request) for f in patient.folders.all())
    )
    if not can_delete:
        return JsonResponse(
            {"success": False, "error": "You do not have permission to delete this patient."},
            status=403,
        )
    patient.deleted = True
    patient.save(update_fields=["deleted"])
    return JsonResponse({"success": True, "message": "Scan deleted successfully"})


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

    if not user_is_project_admin(request.user, "brain"):
        return JsonResponse(
            {"success": False, "error": "You do not have permission to bulk delete scans."},
            status=403,
        )

    deleted_count = Patient.objects.filter(patient_id__in=scan_ids).update(deleted=True)
    if not deleted_count:
        return JsonResponse({"success": False, "error": "No valid scans found to delete"}, status=404)

    return JsonResponse(
        {
            "success": True,
            "message": f"Successfully deleted {deleted_count} scans.",
            "deleted_count": deleted_count,
        }
    )


@login_required
@require_POST
def bulk_purge_patients(request):
    """Permanently delete patients: removes their stored files from object
    storage, then hard-deletes the Patient rows (cascades to FileRegistry/Job)."""
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON payload"}, status=400)

    scan_ids = data.get("scan_ids", [])
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse({"success": False, "error": "scan_ids list is required"}, status=400)

    if not user_is_project_admin(request.user, "brain"):
        return JsonResponse(
            {"success": False, "error": "You do not have permission to permanently delete scans."},
            status=403,
        )

    patients = Patient.objects.filter(patient_id__in=scan_ids)
    found_ids = list(patients.values_list("patient_id", flat=True))
    if not found_ids:
        return JsonResponse({"success": False, "error": "No valid scans found to delete"}, status=404)

    storage = get_object_storage()
    file_paths = list(
        FileRegistry.objects.filter(brain_patient_id__in=found_ids).values_list("file_path", flat=True)
    )
    storage_errors = []
    for file_path in file_paths:
        try:
            storage.delete(file_path)
        except Exception as exc:
            logger.exception("Error deleting object storage file %s", file_path)
            storage_errors.append(file_path)

    deleted_count, _ = patients.delete()

    response = {
        "success": True,
        "message": f"Permanently deleted {len(found_ids)} scan(s) and {len(file_paths)} file(s).",
        "deleted_count": len(found_ids),
        "files_deleted": len(file_paths) - len(storage_errors),
    }
    if storage_errors:
        response["storage_errors"] = storage_errors
    return JsonResponse(response)


@login_required
@require_POST
def rerun_processing(request, patient_id):
    return JsonResponse(
        {"success": False, "error": "Brain processing rerun is not configured."},
        status=400,
    )


@login_required
@require_POST
def bulk_rerun_processing(request):
    return JsonResponse(
        {"success": False, "error": "Brain bulk processing rerun is not configured."},
        status=400,
    )


@login_required
def user_profile(request, username=None):
    return render(request, "brain/profile.html", {"profile_user": request.user})


@login_required
@require_POST
def create_folder(request):
    try:
        if not user_is_project_admin(request.user, "brain"):
            return JsonResponse({"error": "Permission denied"}, status=403)

        data = _json.loads(request.body) if request.body else request.POST
        name = (data.get("name") or "").strip()
        if not name:
            return JsonResponse({"error": "Folder name is required"}, status=400)

        folder, created = Folder.objects.get_or_create(
            name=name,
            parent=None,
            defaults={"created_by": request.user},
        )
        return JsonResponse(
            {
                "success": True,
                "folder": {
                    "id": folder.id,
                    "name": folder.name,
                    "path": folder.name,
                    "created": created,
                },
            }
        )
    except Exception as exc:
        logger.exception("Error creating brain folder")
        return JsonResponse({"error": str(exc)}, status=500)


@login_required
def folder_stats(request, folder_id):
    folder = get_object_or_404(Folder, id=folder_id)
    return JsonResponse(
        {
            "success": True,
            "folder": {"id": folder.id, "name": folder.name},
            "stats": {"patient_count": folder.patients.count()},
        }
    )


@login_required
@require_POST
def rename_folder(request, folder_id):
    if not user_is_project_admin(request.user, "brain"):
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
    if not user_is_project_admin(request.user, "brain"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    folder = get_object_or_404(Folder, id=folder_id)

    patient_count = folder.patients.count()
    force = request.GET.get("force") == "true"
    if patient_count and not force:
        return JsonResponse(
            {
                "success": False,
                "error": (
                    f"Folder still contains {patient_count} patient(s). "
                    "Move or delete them first, or pass ?force=true to delete the folder anyway."
                ),
            },
            status=400,
        )

    folder.delete()
    return JsonResponse({"success": True})


@login_required
def folder_permissions(request, folder_id):
    folder = get_object_or_404(Folder, id=folder_id)
    if not user_is_project_admin(request.user, "brain"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    rows = folder.access_list.select_related("user").order_by("user__username")
    users = User.objects.filter(is_active=True).order_by("username")
    return JsonResponse(
        {
            "success": True,
            "folder": {"id": folder.id, "name": folder.name},
            "permissions": [
                {"user_id": row.user_id, "username": row.user.username, "role": row.role}
                for row in rows
            ],
            "users": [{"id": user.id, "username": user.username} for user in users],
        }
    )


@login_required
@require_POST
def upsert_folder_permission(request, folder_id):
    if not user_is_project_admin(request.user, "brain"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    folder = get_object_or_404(Folder, id=folder_id)
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON payload"}, status=400)
    user_id = data.get("user_id")
    role = data.get("role")
    valid_roles = {choice[0] for choice in FolderAccess.ROLE_CHOICES}
    if role not in valid_roles:
        return JsonResponse({"success": False, "error": "Invalid role"}, status=400)
    if not user_id:
        return JsonResponse({"success": False, "error": "user_id required"}, status=400)
    row, _ = FolderAccess.objects.update_or_create(
        folder=folder,
        user_id=user_id,
        defaults={"role": role},
    )
    return JsonResponse({"success": True, "user_id": row.user_id, "role": row.role})


@login_required
@require_http_methods(["DELETE"])
def delete_folder_permission(request, folder_id, user_id):
    if not user_is_project_admin(request.user, "brain"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    folder = get_object_or_404(Folder, id=folder_id)
    FolderAccess.objects.filter(folder=folder, user_id=user_id).delete()
    return JsonResponse({"success": True})


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
    if not user_is_project_admin(request.user, "brain"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    folder = None
    if folder_id and folder_id not in ("root", "all"):
        folder = get_object_or_404(Folder, id=folder_id)
    patients = Patient.objects.filter(patient_id__in=scan_ids)
    updated = 0
    for patient in patients:
        patient.folders.set([folder] if folder else [])
        updated += 1
    return JsonResponse({"success": True, "updated": updated})


@login_required
@require_POST
def add_patients_to_folder(request):
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON payload"}, status=400)
    scan_ids = data.get("scan_ids", [])
    folder_id = data.get("folder_id")
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse({"success": False, "error": "scan_ids list is required"}, status=400)
    if not folder_id or folder_id in ("root", "all"):
        return JsonResponse({"success": False, "error": "A specific folder_id is required"}, status=400)
    if not user_is_project_admin(request.user, "brain"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    folder = get_object_or_404(Folder, id=folder_id)
    patients = Patient.objects.filter(patient_id__in=scan_ids)
    updated = 0
    for patient in patients:
        patient.folders.add(folder)
        updated += 1
    return JsonResponse({"success": True, "updated": updated})


@login_required
@require_POST
def remove_patients_from_folder(request):
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON payload"}, status=400)
    scan_ids = data.get("scan_ids", [])
    folder_id = data.get("folder_id")
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse({"success": False, "error": "scan_ids list is required"}, status=400)
    if not folder_id or folder_id in ("root", "all"):
        return JsonResponse({"success": False, "error": "A specific folder_id is required"}, status=400)
    if not user_is_project_admin(request.user, "brain"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    folder = get_object_or_404(Folder, id=folder_id)
    patients = Patient.objects.filter(patient_id__in=scan_ids)
    updated = 0
    for patient in patients:
        patient.folders.remove(folder)
        updated += 1
    return JsonResponse({"success": True, "updated": updated})


@login_required
@require_POST
def add_patient_tag(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not (
        user_is_project_admin(request.user, "brain")
        or any(user_can_write_annotations(request.user, f, request) for f in patient.folders.all())
    ):
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
    if not (
        user_is_project_admin(request.user, "brain")
        or any(user_can_write_annotations(request.user, f, request) for f in patient.folders.all())
    ):
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
def upload_voice_caption(request, patient_id):
    return JsonResponse({"error": "Voice captions are handled by text captions for Brain."}, status=400)


@login_required
@require_POST
def upload_text_caption(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    text = data.get("text") or data.get("caption") or ""
    if not text.strip():
        return JsonResponse({"error": "Caption text is required"}, status=400)
    caption = patient.voice_captions.create(
        user=request.user,
        duration=0,
        text_caption=text.strip(),
        original_text_caption=text.strip(),
        processing_status="completed",
    )
    return JsonResponse(
        {
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
        }
    )


@login_required
def delete_voice_caption(request, patient_id, caption_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    caption = get_object_or_404(patient.voice_captions, id=caption_id)

    is_owner = caption.user_id == request.user.id
    is_admin = user_is_project_admin(request.user, "brain")
    if not is_owner and not is_admin:
        return JsonResponse(
            {
                "error": "You cannot delete voice captions created by other users.",
                "code": "not_owner",
            },
            status=403,
        )

    if is_admin and not is_owner:
        data = _json.loads(request.body) if request.body else {}
        if not data.get("admin_confirmed"):
            return JsonResponse(
                {
                    "error": "Admin confirmation required",
                    "code": "admin_confirmation_required",
                    "message": f"You are about to delete a voice caption created by {caption.user.username}. Please confirm this action.",
                },
                status=403,
            )

    caption.delete()
    return JsonResponse({"success": True})


@login_required
@require_POST
def edit_voice_caption_transcription(request, patient_id, caption_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    caption = get_object_or_404(patient.voice_captions, id=caption_id)

    if not user_can_edit_caption(request.user, caption):
        return JsonResponse(
            {
                "error": "You do not have permission to edit this transcription.",
                "code": "permission_denied",
            },
            status=403,
        )

    try:
        data = _json.loads(request.body) if request.body else {}
        action = data.get("action")

        if action == "edit":
            new_text = (data.get("text") or "").strip()
            if not new_text:
                return JsonResponse({"error": "Transcription text cannot be empty"}, status=400)
            caption.edit_transcription(new_text, request.user)
        elif action == "revert":
            caption.revert_to_original(request.user)
        else:
            return JsonResponse({"error": 'Invalid action. Use "edit" or "revert"'}, status=400)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)

    return JsonResponse(
        {
            "success": True,
            "caption": {
                "id": caption.id,
                "text_caption": caption.text_caption,
                "is_edited": caption.is_edited,
                "edit_history": caption.edit_history,
            },
        }
    )


@login_required
def update_voice_caption_modality(request, patient_id, caption_id):
    return JsonResponse({"error": "Modality is not used for brain voice captions."}, status=400)


def _file_payload(file_obj):
    return {"id": file_obj.id, "file_type": file_obj.file_type, "file_path": file_obj.file_path}


@login_required
def patient_viewer_data(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    return JsonResponse({"patient_id": patient.patient_id, "files": [_file_payload(item) for item in patient.files.all()]})


patient_cbct_data = patient_viewer_data
patient_panoramic_data = patient_viewer_data
patient_intraoral_data = patient_viewer_data
patient_intraoral_segmentation_data = patient_viewer_data
patient_teleradiography_data = patient_viewer_data


@login_required
def update_patient_intraoral_segmentation(request, patient_id):
    return JsonResponse({"error": "Not available for Brain."}, status=400)


@login_required
def patient_volume_data(request, patient_id, modality_slug):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    file_obj = patient.files.filter(modality__slug=modality_slug).order_by("-created_at").first()
    if not file_obj:
        return JsonResponse({"error": "File not found"}, status=404)
    return JsonResponse(_file_payload(file_obj))


@login_required
def get_nifti_metadata(request, patient_id):
    return JsonResponse({"metadata": {}})


@login_required
def update_nifti_metadata(request, patient_id):
    return JsonResponse({"ok": True})


def _brain_shared_export_availability(share_token):
    """Resolve a brain export by share token and whether it's downloadable."""
    export = Export.objects.filter(share_token=share_token).first()
    if not export:
        return None, False
    if export.share_mode == "private":
        return export, False
    if export.status != "completed":
        return export, False
    if not export.file_path or not artifact_exists(export.file_path):
        return export, False
    return export, True


@login_required
@_with_brain_export_mappings
def export_list(request):
    """Export history page. Reuses the maxillo template with ns='brain'."""
    exports = Export.objects.filter(user=request.user).order_by("-created_at")

    exports_with_sizes = [
        {
            "export": export,
            "size_display": format_file_size(export.file_size) if export.file_size else None,
        }
        for export in exports
    ]

    paginator = Paginator(exports_with_sizes, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "maxillo/export_list.html",
        {"exports": page_obj, "page_obj": page_obj, "ns": "brain"},
    )


@login_required
@_with_brain_export_mappings
def export_new(request):
    """Create-export page. Reuses the maxillo template with ns='brain'."""
    if request.method == "POST":
        folder_ids = [int(fid) for fid in request.POST.getlist("folder_ids")]
        modality_slugs = request.POST.getlist("modality_slugs")

        if not folder_ids:
            messages.error(request, "Please select at least one folder.")
            return redirect("brain:export_new")
        if not modality_slugs:
            messages.error(request, "Please select at least one modality.")
            return redirect("brain:export_new")

        filters = {
            key.replace("filter_", ""): True
            for key in request.POST.keys()
            if key.startswith("filter_")
        }
        include_raw, include_processed = _resolve_content_selection(
            request.POST, default_when_missing=False
        )
        include_reports = _coerce_bool(request.POST.get("include_reports"), default=False)

        if not include_raw and not include_processed and not include_reports:
            messages.error(
                request,
                "Please select at least one content type: Raw files, Processed files, and/or Reports.",
            )
            return redirect("brain:export_new")

        query_params = {
            "domain": "brain",
            "folder_ids": folder_ids,
            "modality_slugs": modality_slugs,
            "filters": filters,
            "include_raw": include_raw,
            "include_processed": include_processed,
            "include_reports": include_reports,
        }

        modality_names = list(
            Modality.objects.filter(slug__in=modality_slugs).values_list("name", flat=True)
        ) or modality_slugs
        selected_content = [
            label
            for label, on in (
                ("Raw", include_raw),
                ("Processed", include_processed),
                ("Reports", include_reports),
            )
            if on
        ]
        query_summary = ", ".join(
            [
                f"{len(folder_ids)} folder{'s' if len(folder_ids) != 1 else ''}",
                " + ".join(modality_names),
                f"Content: {' + '.join(selected_content)}",
            ]
        )

        export = Export.objects.create(
            user=request.user,
            status="pending",
            query_params=query_params,
            query_summary=query_summary,
        )

        start_export_processing(export.id, "brain")
        messages.success(request, f"Export #{export.id} created and processing started.")
        return redirect("brain:export_list")

    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(parent__isnull=True).order_by("name"),
        "brain",
    )
    folders_with_counts = [
        {"folder": folder, "patient_count": folder.patients.count()} for folder in folders
    ]
    modalities = Modality.objects.filter(projects__slug="brain", is_active=True).order_by("name")
    return render(
        request,
        "maxillo/export_new.html",
        {"folders": folders_with_counts, "modalities": modalities, "ns": "brain"},
    )


@login_required
@_with_brain_export_mappings
def export_preview(request):
    """AJAX export statistics. Reuses the generalized ExportProcessor for brain."""
    try:
        if request.method == "POST":
            data = _json.loads(request.body) if request.body else {}
        else:
            data = request.GET

        folder_ids = data.get("folder_ids", [])
        if isinstance(folder_ids, str):
            folder_ids = [int(fid) for fid in folder_ids.split(",") if fid]
        else:
            folder_ids = [int(fid) for fid in folder_ids if fid]

        modality_slugs = data.get("modality_slugs", [])
        if isinstance(modality_slugs, str):
            modality_slugs = modality_slugs.split(",") if modality_slugs else []

        include_raw, include_processed = _resolve_content_selection(data)
        query_params = {
            "domain": "brain",
            "folder_ids": folder_ids,
            "modality_slugs": modality_slugs,
            "filters": data.get("filters", {}),
            "include_raw": include_raw,
            "include_processed": include_processed,
            "include_reports": _coerce_bool(data.get("include_reports"), default=False),
        }

        proc = ExportProcessor(
            Export(user=request.user, query_params=query_params), domain="brain"
        )
        patients = proc.query_patients()
        patient_count = patients.count()
        if patient_count:
            files, total_size = proc.collect_files(patients)
            file_count = len(files)
        else:
            file_count, total_size = 0, 0

        return JsonResponse(
            {
                "success": True,
                "patient_count": patient_count,
                "folder_count": len(folder_ids),
                "modality_count": len(modality_slugs),
                "file_count": file_count,
                "estimated_size": format_file_size(total_size),
                "estimated_size_bytes": total_size,
            }
        )
    except Exception as e:
        logger.error(f"Error in brain export_preview: {e}", exc_info=True)
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
def export_status(request, export_id):
    """AJAX status endpoint polled by the export list page."""
    export = get_object_or_404(Export, id=export_id)
    if export.user != request.user and not user_is_project_admin(request.user, "brain"):
        return JsonResponse({"error": "Permission denied"}, status=403)

    export = _recover_stuck_export(export)

    data = {
        "id": export.id,
        "status": export.status,
        "query_summary": export.query_summary,
    }
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

    if export.user != request.user and not user_is_project_admin(request.user, "brain"):
        messages.error(request, "You do not have permission to download this export.")
        return redirect("brain:export_list")

    if export.status != "completed":
        messages.error(request, "Export is not yet completed.")
        return redirect("brain:export_list")

    if not export.file_path or not artifact_exists(export.file_path):
        messages.error(request, "Export file not found.")
        export.mark_failed("Export file not found in storage")
        return redirect("brain:export_list")

    filename = (
        os.path.basename((export.file_path or "").rstrip("/"))
        or f"export_{export.id}.zip"
    )
    return streaming_response(
        path_or_key=export.file_path,
        content_type="application/zip",
        filename=filename,
        as_attachment=True,
    )


@login_required
@require_POST
def export_share_update(request, export_id):
    export = get_object_or_404(Export, id=export_id)

    if export.user != request.user and not user_is_project_admin(request.user, "brain"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if export.status != "completed":
        return JsonResponse(
            {"success": False, "error": "Only completed exports can be shared"}, status=400
        )

    try:
        data = _json.loads(request.body) if request.body else request.POST
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid JSON payload"}, status=400)

    share_mode = (data.get("share_mode") or "").strip()
    if share_mode not in ("private", "authenticated", "public"):
        return JsonResponse({"success": False, "error": "Invalid share mode"}, status=400)

    regenerate_raw = data.get("regenerate", False)
    regenerate = (
        regenerate_raw
        if isinstance(regenerate_raw, bool)
        else str(regenerate_raw).lower() in ("1", "true", "yes")
    )

    export.share_mode = share_mode
    if share_mode == "private":
        export.share_token = None
        export.shared_at = None
        export.save(update_fields=["share_mode", "share_token", "shared_at"])
        return JsonResponse(
            {"success": True, "share_mode": export.share_mode, "share_url": None}
        )

    if regenerate or not export.share_token:
        export.ensure_share_token(force_new=regenerate)
    export.shared_at = timezone.now()
    export.save(update_fields=["share_mode", "shared_at"])

    return JsonResponse(
        {
            "success": True,
            "share_mode": export.share_mode,
            "share_url": _build_shared_download_url(request, export.share_token),
        }
    )


@require_http_methods(["GET"])
def export_shared_landing(request, share_token):
    export, is_available = _brain_shared_export_availability(share_token)
    if (
        export
        and export.share_mode == "authenticated"
        and not request.user.is_authenticated
    ):
        return redirect_to_login(request.get_full_path())
    return render(
        request,
        "maxillo/export_shared_landing.html",
        {
            "ns": "brain",
            "export": export,
            "is_available": is_available,
            "share_token": share_token,
            "file_size_human": format_file_size(export.file_size)
            if export and export.file_size
            else None,
        },
    )


@require_http_methods(["GET"])
def export_shared_download(request, share_token):
    export, is_available = _brain_shared_export_availability(share_token)
    if not export or not is_available:
        raise Http404("Export is not available.")
    if export.share_mode == "authenticated" and not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    filename = (
        os.path.basename((export.file_path or "").rstrip("/"))
        or f"export_{export.id}.zip"
    )
    return streaming_response(
        path_or_key=export.file_path,
        content_type="application/zip",
        filename=filename,
        as_attachment=True,
    )


@login_required
@require_POST
def export_delete(request, export_id):
    export = get_object_or_404(Export, id=export_id)
    if export.user != request.user and not user_is_project_admin(request.user, "brain"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    file_path = export.file_path
    deleted_count, _ = Export.objects.filter(id=export_id).delete()
    if not deleted_count:
        return JsonResponse(
            {"success": False, "error": "Export not found or already deleted."}, status=404
        )

    if file_path:
        try:
            get_object_storage().delete(file_path)
        except Exception as e:
            logger.warning(f"Could not delete export file {file_path}: {e}")

    return JsonResponse({"success": True})


@login_required
@require_POST
def export_stop(request, export_id):
    """Stop a processing/pending export: kill worker and delete partial ZIPs."""
    export = get_object_or_404(Export, id=export_id)
    if export.user != request.user and not user_is_project_admin(request.user, "brain"):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    if export.status not in {"processing", "pending"}:
        return JsonResponse(
            {"success": False, "error": f"Export is not running (status: {export.status})."},
            status=409,
        )

    killed_pids = _kill_export_processes(export.id)

    deleted_keys = []
    warnings = []
    storage = get_object_storage()

    if export.file_path:
        try:
            storage.delete(export.file_path)
            deleted_keys.append(export.file_path)
        except Exception as e:
            warnings.append(f"Could not delete {export.file_path}: {e}")

    prefix = f"exports/export_{export.id}_"
    try:
        for key in storage.list_keys(prefix):
            if not key.startswith(prefix) or not key.endswith(".zip"):
                continue
            try:
                storage.delete(key)
                deleted_keys.append(key)
            except Exception as e:
                warnings.append(f"Could not delete {key}: {e}")
    except Exception as e:
        warnings.append(f"Could not list keys for prefix {prefix}: {e}")

    who = getattr(request.user, "username", "unknown")
    stopped_at = timezone.now().strftime("%Y-%m-%d %H:%M:%S %Z")
    message = f"Stopped manually by {who} at {stopped_at}."
    if killed_pids:
        message += f" Killed worker PID(s): {', '.join(str(p) for p in killed_pids)}."
    if deleted_keys:
        message += f" Deleted {len(set(deleted_keys))} ZIP object(s)."
    export.mark_failed(message)

    return JsonResponse(
        {
            "success": True,
            "killed_pids": killed_pids,
            "deleted_keys": sorted(set(deleted_keys)),
            "warnings": warnings,
            "status": "failed",
            "error_message": message,
        }
    )
