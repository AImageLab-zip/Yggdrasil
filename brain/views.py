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
from django.http import JsonResponse

from common.file_access import exists as artifact_exists
from common.models import Job, Modality, Project, ProjectAccess
from common.permissions import (
    filter_folders_for_user,
    filter_patients_for_user,
    user_can_delete_single_patient,
    user_can_read_folder,
    user_can_write_annotations,
    user_is_project_admin,
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
        "folders": [{"folder": folder, "patient_count": patients.filter(folders=folder).count()} for folder in folders],
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
                patient.folders.set([folder])

            patient.save()
            patient_upload_form.instance = patient
            patient_upload_form.save(commit=True)

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
    updated = Patient.objects.filter(patient_id__in=scan_ids).update(folder=folder)
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
    patient.voice_captions.filter(id=caption_id, user=request.user).delete()
    return JsonResponse({"success": True})


@login_required
def edit_voice_caption_transcription(request, patient_id, caption_id):
    return JsonResponse({"success": True})


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


@login_required
@_with_brain_export_mappings
def export_list(request):
    exports = Export.objects.filter(user=request.user).order_by("-created_at")
    return render(request, "brain/export_list.html", {"exports": exports})


@login_required
@_with_brain_export_mappings
def export_new(request):
    if request.method == "POST":
        export = Export.objects.create(user=request.user, status="pending", query_params=dict(request.POST), query_summary="Brain export")
        messages.success(request, f"Export #{export.id} created.")
        return redirect("brain:export_list")
    folders = filter_folders_for_user(request.user, Folder.objects.filter(parent__isnull=True), "brain")
    modalities = Modality.objects.filter(projects__slug="brain", is_active=True)
    return render(request, "brain/export_new.html", {"folders": folders, "modalities": modalities})


@login_required
def export_preview(request):
    return JsonResponse({"files": []})


@login_required
def export_status(request, export_id):
    export = get_object_or_404(Export, id=export_id)
    return render(request, "brain/export_status.html", {"export": export})


@login_required
def export_download(request, export_id):
    return JsonResponse({"error": "Brain export download is not implemented in the decoupled view yet."}, status=501)


@login_required
def export_share_update(request, export_id):
    return JsonResponse({"ok": True})


def export_shared_landing(request, share_token):
    return JsonResponse({"share_token": share_token})


def export_shared_download(request, share_token):
    return JsonResponse({"error": "Brain shared export download is not implemented."}, status=501)


@login_required
def export_delete(request, export_id):
    Export.objects.filter(id=export_id, user=request.user).delete()
    return redirect("brain:export_list")
