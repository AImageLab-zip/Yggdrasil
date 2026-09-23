"""Brain views."""

import json as _json
import logging
import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.http import JsonResponse

from common.annotation_lock import annotation_lock_reasons, lock_message
from common.domains import landing_domain_cards, order_projects_for_landing
from common.export_share import is_share_expired
from common.file_access import exists as artifact_exists
from common.modality_config import (
    modality_status,
    rerun_step_labels,
    rerunnable_steps_for_patient,
)
from common.project_filters import presence_filter_specs
from common.models import FileRegistry, Modality, Project, ProjectAccess
from common.object_storage import get_object_storage
from common.permissions import current_project as session_project
from common.permissions import (
    filter_folders_for_user,
    filter_patients_for_user,
    user_can_delete_single_patient,
    user_can_edit_caption,
    user_can_view_caption_content,
    user_can_write_patient_annotations,
    get_patient_for,
    user_has_project_access,
    user_is_project_admin,
)

from common.rerun import bulk_rerun_steps, describe, rerun_steps_for_patient
from .export_config import install_brain_export_mappings
from .file_utils import save_brain_modality_file
from .forms import PatientForm, PatientManagementForm, PatientUploadForm
from common.view_helpers import patient_list_response, upload_error_response
from .helpers import redirect_with_namespace, render_with_fallback
from .models import Export, Folder, Patient, Tag


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
            "continue_url": "/brain/" if current_project_name else None,
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
    return redirect("brain:patient_list")


@login_required
def patient_detail(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    can_view = bool(patient.project and user_has_project_access(request.user, patient.project))
    if user_is_project_admin(request.user, patient.project):
        can_view = True
    if not can_view:
        messages.error(request, "You do not have permission to view this scan.")
        return redirect("brain:patient_list")

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

    # The step picker reads the FileRegistry rows; `patient_files` below is the
    # template's display shape, not those rows.
    patient_file_rows = list(patient.files.all())
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

    # Viewers and admins see all captions; annotators see only their own (to
    # avoid bias during annotation). Through the canonical helpers -- this used
    # to inline "admin or author", which ghosted every caption for the
    # project's own viewers, the public-demo guest among them.
    voice_captions = patient.voice_captions.all()
    is_admin_user = user_is_project_admin(request.user, patient.project)
    for caption in voice_captions:
        caption.can_view_content = user_can_view_caption_content(request.user, caption)
        caption.can_edit_content = user_can_edit_caption(request.user, caption)
        caption.is_ghost = not caption.can_view_content

    allowed_modalities = list(Modality.objects.filter(projects__id=request.session.get("current_project_id"), is_active=True))
    if not allowed_modalities:
        allowed_modalities = list(Modality.objects.filter(is_active=True))

    _brain_raw_lock_reasons = annotation_lock_reasons(patient)

    context = {
        "patient": patient,
        "user_profile": request.user.profile,
        "management_form": management_form,
        "has_cbct": False,
        "has_uploaded_panoramic": False,
        "can_modify_segmentation": can_modify,
        "can_create_caption": can_modify,
        "patient_modalities": patient_modalities,
        "default_modality_slug": next((m["slug"] for m in patient_modalities if m["slug"] != "braintumor-mri-seg"), None),
        # Structured payloads rendered via |json_script (XSS-safe, no |safe needed)
        "django_data": {
            "canEdit": bool(can_modify),
            "scanId": patient.patient_id,
            "hasIOS": bool(getattr(patient, "has_ios_scans", False)),
            "hasCBCT": False,
            "isCBCTProcessed": bool(getattr(patient, "is_cbct_processed", False)),
            "modalities": patient_modalities,
            "defaultModality": next((m["slug"] for m in patient_modalities if m["slug"] != "braintumor-mri-seg"), None),
        },
        "viewer_grid_data": {
            "scanId": patient.patient_id,
            "projectNamespace": (request.resolver_match.namespace if request.resolver_match else None) or "brain",
            "modalityFiles": modality_files,
            "segmentationFile": segmentation_file,
            # Stated rather than left to the client's default. Brain is the surface
            # drag-and-drop exists for -- four co-registered sequences and no single
            # primary -- and the flag now decides two things: that the chips are bound,
            # and that the four windows start *empty* rather than showing one
            # arbitrarily-chosen series four times over.
            # maxillo/views/patient_detail.py sets it False for the CBCT grid, whose
            # windows are three fixed planes and a render.
            "enableDragDrop": True,
        },
        "patient_files": patient_files,
        # Brain has no add/remove raw controls of its own, but it renders the
        # shared file-management section, so it carries the same lock banner.
        "raw_data_locked": bool(_brain_raw_lock_reasons),
        "raw_lock_message": lock_message(_brain_raw_lock_reasons),
        "voice_captions": voice_captions,
        "is_admin_user": is_admin_user,
        "modality_files": modality_files,
        "segmentation_file": segmentation_file,
        "rerunnable_step_slugs": [
            step["slug"]
            for step in rerunnable_steps_for_patient(
                patient_file_rows, patient_modalities, patient=patient
            )
            if step["slug"] not in ("rawzip", "braintumor-mri-seg")
        ],
        # Checkbox labels for the shared rerun picker (common/partials/rerun_modal.html).
        "rerun_step_labels": rerun_step_labels(patient_files, patient_modalities),
        "allowed_modalities": allowed_modalities,
        "allowed_modality_slugs": [m.slug for m in allowed_modalities],
        # report_language now provided globally by common.context_processors.user_prefs
    }

    # Annotation methods + sidebar default tab (captions pane hides when the
    # project disables voice_caption).
    allowed_annotations = []
    if getattr(patient, "project", None) is not None:
        allowed_annotations = list(
            patient.project.annotation_methods
            .filter(is_active=True)
            .values_list("slug", flat=True)
        )
    captions_enabled = "voice_caption" in allowed_annotations
    context["allowed_annotations"] = allowed_annotations
    context["captions_enabled"] = captions_enabled
    context["default_tab"] = "captions" if captions_enabled else "files"
    context["occlusion_enabled"] = False

    # Record for the landing "Continue where you left off" strip (best-effort).
    from common.activity import record_recent
    record_recent(
        request.user, "brain", patient.patient_id,
        patient_name=getattr(patient, "name", "") or "",
        project_label="Brain",
    )
    return render_with_fallback(request, "patient_detail", context)


@login_required
def patient_list(request):
    patients = Patient.objects.select_related("uploaded_by").prefetch_related(
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

    patients = patients.order_by("-uploaded_at")
    allowed_modalities = []
    current_project = None
    if current_project_id:
        project = (
            Project.objects.filter(id=current_project_id)
            .prefetch_related("modalities", "annotation_methods")
            .first()
        )
        if project:
            current_project = project
            allowed_modalities = list(project.modalities.filter(is_active=True))

    status_filters = {}
    for modality in allowed_modalities:
        slug = modality.slug or ""
        if slug and slug != "rawzip":
            value = request.GET.get(f"status_{slug}", "").strip()
            if value in {"processed", "processing", "failed"}:
                status_filters[slug] = value

    patients_with_status = []
    # UI flag for the project the list is showing; each row is checked on its own project.
    is_admin = user_is_project_admin(request.user, session_project(request))
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
            status = modality_status(
                slug,
                jobs_by_modality.get(slug, []),
                bool(files_by_modality.get(slug)),
            )
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
            "folder": patient.folder,
            "available_modalities": [m.slug for m in patient.modalities.all()],
            "modality_statuses": {item["slug"]: item["status"] for item in modality_status_list},
            "modality_status_list": modality_status_list,
            "rerunnable_steps": rerunnable_steps_for_patient(patient_files, modality_status_list, patient=patient),
            "can_delete": bool(user_is_project_admin(request.user, patient.project) or user_can_delete_single_patient(request.user, patient.folder, patient.project)),
        })

    if status_filters:
        patients_with_status = [
            item for item in patients_with_status
            if all(item["modality_statuses"].get(slug, "absent") == value for slug, value in status_filters.items())
        ]

    try:
        per_page = int(request.GET.get("per_page", 10))
    except (TypeError, ValueError):
        per_page = 10
    if per_page not in {10, 20, 50, 100}:
        per_page = 10
    page_obj = Paginator(patients_with_status, per_page).get_page(request.GET.get("page"))
    project_id = current_project_id
    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(parent__isnull=True)
        .filter(project_id=project_id if project_id else None)
        .order_by("name"),
        "brain",
    )
    projects_for_sidebar = Project.objects.filter(domain="brain", is_active=True)
    if not request.user.is_staff:
        accessible_project_ids = ProjectAccess.objects.filter(
            user=request.user
        ).values_list("project_id", flat=True)
        projects_for_sidebar = projects_for_sidebar.filter(id__in=accessible_project_ids)
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
        # Shared filter bar: only the annotations this project collects. Brain
        # collects voice captions; the maxillo-only entries never apply here
        # because their annotation methods are not enabled on brain projects.
        "presence_filter_specs": presence_filter_specs(
            request, current_project, {m.slug for m in allowed_modalities}
        ),
        "allowed_modalities": allowed_modalities,
        "status_filters": status_filters,
        "modality_filter_specs": [
            {"slug": m.slug, "name": m.name, "icon": m.icon or "", "label": m.label or "", "value": status_filters.get(m.slug, "")}
            for m in allowed_modalities
            if m.slug != "rawzip"
        ],
        "rerun_step_labels": rerun_step_labels(
            None,
            [
                {"slug": m.slug, "name": m.name, "label": m.label or "", "status": None}
                for m in allowed_modalities
                if m.slug != "rawzip"
            ],
        ),
    }
    return render_with_fallback(request, "patient_list", context)


def _first_form_error(form):
    """One line for the uploader's toast: the first error the form reported."""
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
    namespace = "brain"

    if not request.user.profile:
        messages.error(request, "You do not have permission to upload scans.")
        return redirect_with_namespace(request, "patient_list")

    if not user_profile.can_upload_scans():
        messages.error(request, "You do not have permission to upload scans.")
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
            allowed_modalities = list(project.modalities.filter(is_active=True).exclude(slug="rawzip"))
        except Project.DoesNotExist:
            pass

    if request.method == "POST":
        patient_upload_form = PatientUploadForm(
            request.POST, request.FILES, user=request.user, current_project=project
        )
        patient_form = PatientForm()

        brain_upload_fields = {
            "braintumor-mri-t1",
            "braintumor-mri-t2",
            "braintumor-mri-flair",
            "braintumor-mri-t1c",
            "braintumor-mri-seg",
        }
        is_xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        has_upload = any(request.FILES.getlist(field_name) for field_name in brain_upload_fields)
        form_is_valid = patient_upload_form.is_valid()
        if form_is_valid and not has_upload:
            patient_upload_form.add_error(None, "Add at least one file before uploading.")
            form_is_valid = False

        if not form_is_valid:
            # A re-rendered form is HTML with status 200, which the XHR uploader
            # cannot tell from success; give it the first error as JSON instead.
            failed = upload_error_response(request, _first_form_error(patient_upload_form))
            if failed is not None:
                return failed

        if form_is_valid:
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
                    denied = "You do not have permission to upload to the selected folder."
                    failed = upload_error_response(request, denied, status=403)
                    if failed is not None:
                        return failed
                    messages.error(request, denied)
                    allowed_folders = filter_folders_for_user(
                        request.user,
                        Folder.objects.filter(parent__isnull=True).order_by("name"),
                        namespace,
                    )
                    return render(request, "common/upload/upload.html", {
                        "patient_form": patient_form,
                        "patient_upload_form": patient_upload_form,
                        "folders": allowed_folders,
                        "allowed_modalities": allowed_modalities,
                    })

            # Project scope is mandatory: assign it before the first save rather
            # than saving the patient twice with an unscoped row in between.
            if project:
                patient.project = project
            if folder:
                patient.folder = folder
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

            return patient_list_response(request)
    else:
        patient_form = PatientForm()
        patient_upload_form = PatientUploadForm(user=request.user, current_project=project)

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
    if not (
        user_is_project_admin(request.user, patient.project)
        or user_can_write_patient_annotations(request.user, patient)
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

    # Authorized per patient against its own project; ids the user does not
    # administer are dropped exactly like ids that do not exist.
    found_ids = [
        patient.patient_id
        for patient in Patient.objects.select_related("project").filter(patient_id__in=scan_ids)
        if user_is_project_admin(request.user, patient.project)
    ]
    patients = Patient.objects.filter(patient_id__in=found_ids)
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
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not (
        user_is_project_admin(request.user, patient.project)
        or (patient.folder and user_can_write_patient_annotations(request.user, patient))
    ):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    try:
        data = _json.loads(request.body) if request.body else {}
    except _json.JSONDecodeError:
        data = {}

    requested_jobs = data.get("jobs")
    if not requested_jobs:
        requested_jobs = list(patient.modalities.values_list("slug", flat=True))

    result = rerun_steps_for_patient(patient, requested_jobs)
    return JsonResponse(
        {
            "success": True,
            "message": describe(result),
            "updated": result["updated"],
            "created": result["created"],
            "not_found": result["not_found"],
        }
    )


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

    patients = Patient.objects.filter(patient_id__in=scan_ids)
    for p in patients:
        if not user_is_project_admin(request.user, p.project):
            return JsonResponse({"success": False, "error": f"Permission denied for patient {p.patient_id}"}, status=403)

    requested_jobs = data.get("jobs")
    if not isinstance(requested_jobs, list) or not requested_jobs:
        return JsonResponse({"success": False, "error": "jobs list is required"}, status=400)

    patients = list(patients)
    result = bulk_rerun_steps(patients, requested_jobs)
    return JsonResponse(
        {
            "success": True,
            "message": (
                f"Reprocessing queued for {result['updated_pairs']} job(s) "
                f"across {len(patients)} scan(s)."
            ),
            "selected_scan_count": len(patients),
            "requested_modalities": result["requested"],
            "updated_pairs": result["updated_pairs"],
            "not_found_pairs": result["not_found_pairs"],
            "created_slugs": result["created_slugs"],
            "updated_by_modality": result["updated_by_modality"],
            "not_found_by_modality": result["not_found_by_modality"],
        }
    )
















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
    folder = get_object_or_404(Folder, id=folder_id)
    if not user_is_project_admin(request.user, folder.project):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    patients = Patient.objects.select_related("project").filter(patient_id__in=scan_ids)
    updated = 0
    for patient in patients:
        if not user_is_project_admin(request.user, patient.project):
            continue
        patient.folder = folder
        patient.project = folder.project
        patient.save(update_fields=["folder", "project"])
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
    folder = get_object_or_404(Folder, id=folder_id)
    if not user_is_project_admin(request.user, folder.project):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    patients = Patient.objects.select_related("project").filter(patient_id__in=scan_ids)
    updated = 0
    for patient in patients:
        if patient.folder_id != folder.id or not user_is_project_admin(request.user, patient.project):
            continue
        # Folders are mandatory: removing a folder falls back to the project's
        # default folder rather than leaving the patient folderless.
        default_folder = Folder.objects.filter(project=patient.project, parent__isnull=True).first()
        patient.folder = default_folder or folder
        patient.save(update_fields=["folder"])
        updated += 1
    return JsonResponse({"success": True, "updated": updated})














def _file_payload(file_obj):
    return {"id": file_obj.id, "file_type": file_obj.file_type, "file_path": file_obj.file_path}


@login_required
def patient_viewer_data(request, patient_id):
    patient = get_patient_for(request.user, Patient, patient_id, "read")
    return JsonResponse({"patient_id": patient.patient_id, "files": [_file_payload(item) for item in patient.files.all()]})


patient_cbct_data = patient_viewer_data
patient_panoramic_data = patient_viewer_data
patient_intraoral_data = patient_viewer_data
patient_teleradiography_data = patient_viewer_data


@login_required
def patient_volume_data(request, patient_id, modality_slug):
    patient = get_patient_for(request.user, Patient, patient_id, "read")
    file_obj = patient.files.filter(modality__slug=modality_slug).order_by("-created_at").first()
    if not file_obj:
        return JsonResponse({"error": "File not found"}, status=404)
    return JsonResponse(_file_payload(file_obj))


@login_required
def get_nifti_metadata(request, patient_id):
    get_patient_for(request.user, Patient, patient_id, "read")
    return JsonResponse({"metadata": {}})


@login_required
def update_nifti_metadata(request, patient_id):
    get_patient_for(request.user, Patient, patient_id, "admin")
    return JsonResponse({"ok": True})


def _brain_shared_export_availability(share_token):
    """Resolve a brain export by share token and whether it's downloadable."""
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
    return (
        Project.objects.filter(id=project_id)
        .prefetch_related("modalities", "annotation_methods", "disabled_steps")
        .first()
    )
















