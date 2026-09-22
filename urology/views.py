"""Urology views."""

import json as _json
import logging
import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.http import require_http_methods, require_POST

from django.middleware.csrf import get_token

from annotations.models import AnnotationSet
from annotations.constants import PayloadFormat
from common.activity import record_recent
from common.export_share import is_share_expired
from common.file_access import exists as artifact_exists
from common.modality_config import (
    modality_status,
    present_modality_slugs,
    rerun_step_labels,
    rerunnable_steps_for_patient,
)
from common.models import FileRegistry, Modality, Project, ProjectAccess
from common.rerun import bulk_rerun_steps, describe, rerun_steps_for_patient
from common.object_storage import get_object_storage
from common.annotation_lock import annotation_lock_reasons, lock_message
from common.permissions import current_project as session_project
from common.permissions import (
    get_patient_for,
    filter_folders_for_user,
    filter_patients_for_user,
    user_can_delete_single_patient,
    user_can_edit_caption,
    user_can_view_caption_content,
    user_can_write_patient_annotations,
    user_has_project_access,
    user_is_project_admin,
)
from common.project_filters import presence_filter_specs

from .export_config import install_urology_export_mappings
from .file_utils import save_urology_modality_file
from .forms import PatientForm, PatientManagementForm, PatientUploadForm
from .helpers import redirect_with_namespace, render_with_fallback
from .models import Export, Folder, Patient, Tag

logger = logging.getLogger(__name__)


def home(request):
    return redirect("urology:patient_list")


@login_required
def select_project(request, project_id):
    project = get_object_or_404(Project, id=project_id, is_active=True)
    if not user_is_project_admin(request.user, project):
        has_access = ProjectAccess.objects.filter(
            user=request.user, project=project
        ).exists()
        if not has_access:
            messages.error(
                request, f"You don't have access to the {project.name} project."
            )
            return redirect("home")
    request.session["current_project_id"] = project.id
    return redirect("urology:patient_list")


@login_required
def patient_list(request):
    patients = (
        Patient.objects.select_related("uploaded_by")
        .prefetch_related(
            "voice_captions",
            "voice_captions__user",
            "tags",
            "modalities",
            "files",
            "files__modality",
            "jobs",
        )
    )
    current_project_id = request.session.get("current_project_id")
    current_project = None
    if current_project_id:
        current_project = (
            Project.objects.filter(id=current_project_id, domain="urology", is_active=True)
            .prefetch_related("modalities", "annotation_methods")
            .first()
        )
    if not current_project:
        current_project = (
            Project.objects.filter(domain="urology", is_active=True)
            .prefetch_related("modalities", "annotation_methods")
            .first()
        )
        if current_project:
            current_project_id = current_project.id
            request.session["current_project_id"] = current_project.id

    if current_project_id and any(field.name == "project" for field in Patient._meta.fields):
        patients = patients.filter(project_id=current_project_id)
    patients = filter_patients_for_user(request.user, patients, "urology")
    patients_for_folder_counts = patients

    search_query = request.GET.get("search", "").strip()
    if search_query:
        patients = patients.filter(
            Q(name__icontains=search_query) | Q(patient_id__icontains=search_query)
        )

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
    # Only what the project registers -- maxillo/views/patient_list.py:226 does the
    # same. Falling back to every urology modality made the status filters and the
    # rerun picker offer modalities the project has not enabled.
    allowed_modalities = (
        list(current_project.modalities.filter(is_active=True).order_by("name"))
        if current_project
        else []
    )

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
            icon = modality.icon or ("fas fa-magnet" if slug == "urology-mri" else "")
            modality_status_list.append({
                "slug": slug,
                "name": modality.name,
                "icon": icon,
                "label": modality.label or "",
                "status": status,
            })
        patients_with_status.append({
            "patient": patient,
            "voice_caption_processing": any(
                vc.processing_status in ["pending", "processing"] for vc in voice_captions
            ),
            "voice_caption_processed": bool(voice_captions)
            and all(vc.processing_status == "completed" for vc in voice_captions),
            "voice_caption_count": len(voice_captions),
            "voice_annotators": list({vc.user.username for vc in voice_captions}),
            "tags": patient.tag_names(),
            "folder": patient.folder,
            "available_modalities": [m.slug for m in patient.modalities.all()],
            "modality_statuses": {item["slug"]: item["status"] for item in modality_status_list},
            "modality_status_list": modality_status_list,
            "rerunnable_steps": rerunnable_steps_for_patient(
                patient_files, modality_status_list, patient=patient
            ),
            "can_delete": bool(
                user_is_project_admin(request.user, patient.project)
                or (
                    patient.folder
                    and user_can_delete_single_patient(
                        request.user, patient.folder, patient.project
                    )
                )
            ),
        })

    if status_filters:
        patients_with_status = [
            item
            for item in patients_with_status
            if all(
                item["modality_statuses"].get(slug, "absent") == value
                for slug, value in status_filters.items()
            )
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
        "urology",
    )
    projects_for_sidebar = Project.objects.filter(domain="urology", is_active=True)
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
        "folders": [
            {
                "folder": folder,
                "patient_count": patients_for_folder_counts.filter(folder=folder).count(),
            }
            for folder in folders
        ],
        "all_tags": Tag.objects.all().order_by("name"),
        "per_page": per_page,
        "user_profile": request.user.profile,
        "is_admin_user": is_admin,
        "has_reports_filter": has_reports_filter,
        "presence_filter_specs": presence_filter_specs(
            request, current_project, {m.slug for m in allowed_modalities}
        ),
        "allowed_modalities": allowed_modalities,
        "status_filters": status_filters,
        "modality_filter_specs": [
            {
                "slug": m.slug,
                "name": m.name,
                "icon": m.icon or ("fas fa-magnet" if m.slug == "urology-mri" else ""),
                "label": m.label or "",
                "value": status_filters.get(m.slug, ""),
            }
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
        "bulk_upload_url": reverse("urology:bulk_upload_patients")
        if (getattr(request.user, "profile", None) and request.user.profile.can_upload_scans())
        else None,
    }
    return render_with_fallback(request, "patient_list", context)


@login_required
def upload_patient(request):
    user_profile = getattr(request.user, "profile", None)
    namespace = "urology"

    if user_profile and not user_profile.can_upload_scans():
        messages.error(request, "You do not have permission to upload scans.")
        return redirect_with_namespace(request, "patient_list")

    current_project_id = request.session.get("current_project_id")
    project = None
    if current_project_id:
        project = (
            Project.objects.prefetch_related("modalities")
            .filter(id=current_project_id, domain=namespace, is_active=True)
            .first()
        )

    if project is None:
        project = Project.objects.filter(domain=namespace, is_active=True).first()
        if project:
            request.session["current_project_id"] = project.id

    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(parent__isnull=True)
        .filter(project=project if project else None)
        .order_by("name"),
        namespace,
    )
    allowed_modalities = []
    if project:
        allowed_modalities = list(
            project.modalities.filter(is_active=True).exclude(slug="rawzip")
        )

    if request.method == "POST":
        patient_upload_form = PatientUploadForm(
            request.POST, request.FILES, user=request.user, current_project=project
        )
        patient_form = PatientForm()

        urology_upload_fields = {
            "urology-mri",
            "urology-wsi",
            "urology-confocal",
        }
        is_xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        has_upload = any(
            request.FILES.getlist(field_name) for field_name in urology_upload_fields
        )
        form_is_valid = patient_upload_form.is_valid()
        if form_is_valid and not has_upload:
            patient_upload_form.add_error(None, "Add at least one file before uploading.")
            form_is_valid = False

        if not form_is_valid and is_xhr:
            error_msg = "Add at least one file before uploading." if not has_upload else "Please fix the errors in the form."
            return JsonResponse({"ok": False, "error": error_msg}, status=400)

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
                    if is_xhr:
                        return JsonResponse(
                            {"ok": False, "error": "You do not have permission to upload to the selected folder."},
                            status=403,
                        )
                    messages.error(
                        request,
                        "You do not have permission to upload to the selected folder.",
                    )
                    allowed_folders = filter_folders_for_user(
                        request.user,
                        Folder.objects.filter(parent__isnull=True).order_by("name"),
                        namespace,
                    )
                    return render(
                        request,
                        "common/upload/upload.html",
                        {
                            "patient_form": patient_form,
                            "patient_upload_form": patient_upload_form,
                            "folders": allowed_folders,
                            "allowed_modalities": allowed_modalities,
                            "bulk_upload_url": reverse("urology:bulk_upload_patients")
                            if (user_profile and user_profile.can_upload_scans())
                            else None,
                        },
                    )

            form_project = patient_upload_form.cleaned_data.get("project")
            if form_project:
                patient.project = form_project
            elif project:
                patient.project = project
                patient_upload_form.cleaned_data["project"] = project
            if folder:
                patient.folder = folder
            patient.save()
            patient_upload_form.instance = patient
            patient_upload_form.save(commit=True)

            uploaded_modalities = []
            processing_job_ids = []
            urology_modalities = {
                "urology-mri": "MRI",
                "urology-wsi": "WSI",
                "urology-confocal": "Confocale",
            }

            for slug, display_name in urology_modalities.items():
                file_obj = request.FILES.get(slug)
                if not file_obj:
                    continue
                try:
                    modality = Modality.objects.get(slug=slug)
                    patient.modalities.add(modality)

                    file_registry, job = save_urology_modality_file(
                        patient, slug, file_obj
                    )
                    if file_registry:
                        uploaded_modalities.append(display_name)
                        if job:
                            processing_job_ids.append(job.id)

                        # Check for optional segmentation file
                        seg_file = request.FILES.get(f"{slug}-segmentation")
                        if seg_file:
                            from .file_utils import save_urology_segmentation_file
                            save_urology_segmentation_file(
                                patient, slug, seg_file, parent_file=file_registry
                            )
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
                    summary_message += (
                        f" Processing jobs: #{', #'.join(str(job_id) for job_id in processing_job_ids)}."
                    )
                messages.success(request, summary_message)
            else:
                messages.success(request, "Patient uploaded successfully!")

            if is_xhr:
                try:
                    redirect_url = reverse(f"{namespace}:patient_list")
                except NoReverseMatch:
                    redirect_url = reverse("patient_list")
                return JsonResponse({"ok": True, "redirect": redirect_url})

            return redirect_with_namespace(request, "patient_list")
    else:
        patient_form = PatientForm()
        patient_upload_form = PatientUploadForm(
            user=request.user, current_project=project
        )

    return render(
        request,
        "common/upload/upload.html",
        {
            "patient_form": patient_form,
            "patient_upload_form": patient_upload_form,
            "folders": folders,
            "allowed_modalities": allowed_modalities,
            "bulk_upload_url": reverse("urology:bulk_upload_patients")
            if (user_profile and user_profile.can_upload_scans())
            else None,
        },
    )


@login_required
def patient_detail(request, patient_id):
    patient = get_object_or_404(Patient, patient_id=patient_id)
    can_view = bool(
        patient.project and user_has_project_access(request.user, patient.project)
    )
    if user_is_project_admin(request.user, patient.project):
        can_view = True
    if not can_view:
        messages.error(request, "You do not have permission to view this scan.")
        return redirect("urology:patient_list")

    management_form = PatientManagementForm(instance=patient, user=request.user)

    can_modify = bool(
        patient.project
        and user_can_write_patient_annotations(request.user, patient)
    )
    if user_is_project_admin(request.user, patient.project):
        can_modify = True

    if request.method == "POST" and can_modify:
        action = request.POST.get("action")
        if action == "update_management":
            management_form = PatientManagementForm(
                request.POST, instance=patient, user=request.user
            )
            if management_form.is_valid():
                management_form.save()
                messages.success(request, "Scan settings updated successfully!")
                return redirect("urology:patient_detail", patient_id=patient_id)

    # The strip lists the project's modalities, not only the ones this patient has
    # uploaded: the page renders a stage panel per modality with its own empty state
    # ("No MRI Scan Uploaded"), so a patient mid-upload still has somewhere to land.
    _strip_modalities = (
        patient.project.modalities.filter(is_active=True).order_by("name")
        if getattr(patient, "project", None) is not None
        else patient.modalities.all().order_by("name")
    )
    patient_modalities = [
        {
            "slug": modality.slug,
            "name": modality.name,
            "label": modality.label or "",
            "subtypes": list(modality.subtypes or []),
        }
        for modality in _strip_modalities
    ]

    # Prepare MRI volume grid data
    modality_files = {}
    mri_file = (
        patient.files.filter(
            modality__slug="urology-mri",
            file_type__in=["urology_mri_raw", "urology_mri_processed"],
        )
        .order_by("-created_at")
        .first()
        or patient.files.filter(
            file_type__in=["urology_mri_raw", "urology_mri_processed"]
        )
        .order_by("-created_at")
        .first()
    )
    if mri_file:
        filename = os.path.basename(mri_file.file_path or "mri.nii.gz")
        modality_files["urology-mri"] = {
            "id": mri_file.id,
            "file_type": mri_file.file_type,
            "filename": filename,
            "file_key": "primary",
        }

    viewer_grid_data = {
        "scanId": patient.patient_id,
        "projectNamespace": "urology",
        "modalityFiles": modality_files,
        "segmentationFile": None,
        "enableDragDrop": False,
        "defaultModality": "urology-mri" if mri_file else None,
        "singleWindowMode": True,
    }

    # Prepare Digital Pathology WSI data
    wsi_file = (
        patient.files.filter(
            modality__slug="urology-wsi",
            file_type__in=["urology_wsi_raw", "urology_wsi_processed"],
        )
        .order_by("-created_at")
        .first()
        or patient.files.filter(
            file_type__in=["urology_wsi_raw", "urology_wsi_processed"]
        )
        .order_by("-created_at")
        .first()
    )
    # Prepare Confocale Microscopy data
    confocal_file = (
        patient.files.filter(
            modality__slug="urology-confocal",
            file_type__in=["urology_confocal_raw", "urology_confocal_processed"],
        )
        .order_by("-created_at")
        .first()
        or patient.files.filter(
            file_type__in=["urology_confocal_raw", "urology_confocal_processed"]
        )
        .order_by("-created_at")
        .first()
    )

    latest_set = (
        AnnotationSet.objects.filter(
            urology_patient=patient, kind="measurements"
        )
        .order_by("-created_at")
        .first()
    )

    def _extract_slide_annotations(slide_file):
        if not slide_file or not latest_set:
            return 0, []
        rev = latest_set.revisions.order_by("-revision_number").first()
        if not rev:
            return 0, []
        expected_revision = rev.revision_number
        payload = rev.payloads.filter(format=PayloadFormat.CORNERSTONE_STATE).first()
        if not payload or not isinstance(payload.data, dict):
            return expected_revision, []
        images = payload.data.get("images")
        if isinstance(images, list):
            for img in images:
                if isinstance(img, dict) and img.get("fileId") == slide_file.id:
                    return expected_revision, img.get("annotations") or []
            return expected_revision, []
        if payload.data.get("fileId") == slide_file.id:
            return expected_revision, payload.data.get("annotations") or []
        return expected_revision, []

    wsi_data = None
    if wsi_file:
        wsi_rev, wsi_annots = _extract_slide_annotations(wsi_file)
        wsi_data = {
            "patientId": patient.patient_id,
            "fileId": wsi_file.id,
            "revision": wsi_rev,
            "annotations": wsi_annots,
            "csrfToken": get_token(request),
            "namespace": "urology",
        }

    confocal_data = None
    if confocal_file:
        confocal_rev, confocal_annots = _extract_slide_annotations(confocal_file)
        confocal_data = {
            "patientId": patient.patient_id,
            "fileId": confocal_file.id,
            "revision": confocal_rev,
            "annotations": confocal_annots,
            "csrfToken": get_token(request),
            "namespace": "urology",
        }

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
        if "_raw" in file_obj.file_type:
            patient_files["raw"].append(file_data)
        elif "_processed" in file_obj.file_type:
            patient_files["processed"].append(file_data)
        else:
            patient_files["other"].append(file_data)

    voice_captions = patient.voice_captions.all()
    is_admin_user = user_is_project_admin(request.user, patient.project)
    for caption in voice_captions:
        caption.can_view_content = user_can_view_caption_content(
            request.user, caption
        )
        caption.can_edit_content = user_can_edit_caption(
            request.user, caption
        )
        caption.is_ghost = not caption.can_view_content

    # The patient's own project decides this, not whichever project the session
    # happens to have open (maxillo/views/patient_detail.py:447), and a project
    # that registers no modality offers none rather than the whole domain.
    raw_allowed_modalities = (
        list(patient.project.modalities.filter(is_active=True).order_by("name"))
        if getattr(patient, "project", None) is not None
        else []
    )
    allowed_modalities = [
        {
            "slug": m.slug,
            "name": getattr(m, "label", "") or m.name.replace("Urology ", ""),
        }
        for m in raw_allowed_modalities
    ]

    # `patient_modalities` is the viewer strip: one panel per *project* modality so
    # a patient mid-upload still has an empty state to land on. The rerun picker is
    # the opposite question -- what can actually be re-run for this patient -- so it
    # takes the same project-scoped list narrowed to the modalities the patient has
    # files for, exactly as maxillo/views/patient_detail.py:500 narrows its own.
    _present_slugs = present_modality_slugs(patient_file_rows)
    rerun_modalities = [m for m in patient_modalities if m["slug"] in _present_slugs]

    _raw_lock_reasons = annotation_lock_reasons(patient)

    context = {
        "patient": patient,
        "user_profile": request.user.profile,
        "management_form": management_form,
        "has_cbct": False,
        "has_uploaded_panoramic": False,
        "can_modify_segmentation": can_modify,
        "can_create_caption": can_modify,
        "patient_modalities": patient_modalities,
        "has_mri": bool(mri_file),
        "has_wsi": bool(wsi_file),
        "has_confocal": bool(confocal_file),
        "mri_file": mri_file,
        "wsi_file": wsi_file,
        "confocal_file": confocal_file,
        "default_modality_slug": "urology-mri" if mri_file else ("urology-wsi" if wsi_file else ("urology-confocal" if confocal_file else None)),
        "django_data": {
            "canEdit": bool(can_modify),
            "scanId": patient.patient_id,
            "hasIOS": False,
            "hasCBCT": False,
            "isCBCTProcessed": False,
            "modalities": patient_modalities,
            "defaultModality": "urology-mri" if mri_file else ("urology-wsi" if wsi_file else ("urology-confocal" if confocal_file else None)),
        },
        "viewer_grid_data": viewer_grid_data,
        "wsi_data": wsi_data,
        "confocal_data": confocal_data,
        "patient_files": patient_files,
        "raw_data_locked": bool(_raw_lock_reasons),
        "raw_lock_message": lock_message(_raw_lock_reasons),
        "voice_captions": voice_captions,
        "is_admin_user": is_admin_user,
        "modality_files": modality_files,
        "rerunnable_step_slugs": [
            step["slug"]
            for step in rerunnable_steps_for_patient(
                patient_file_rows, rerun_modalities, patient=patient
            )
            if step["slug"] != "rawzip"
        ],
        # Checkbox labels for the shared rerun picker (common/partials/rerun_modal.html).
        "rerun_step_labels": rerun_step_labels(patient_file_rows, rerun_modalities),
        "allowed_modalities": allowed_modalities,
        "allowed_modality_slugs": [
            m["slug"] if isinstance(m, dict) else m.slug for m in allowed_modalities
        ],
    }

    allowed_annotations = []
    if getattr(patient, "project", None) is not None:
        allowed_annotations = list(
            patient.project.annotation_methods.filter(is_active=True).values_list(
                "slug", flat=True
            )
        )
    captions_enabled = "voice_caption" in allowed_annotations
    context["allowed_annotations"] = allowed_annotations
    context["captions_enabled"] = captions_enabled
    # Now that captions_enabled is real, the default tab has to follow it or the
    # page opens on a pane the project has disabled (brain/views.py:256).
    context["default_tab"] = "captions" if captions_enabled else "files"

    record_recent(
        request.user,
        "urology",
        patient.patient_id,
        patient_name=getattr(patient, "name", "") or "",
        project_label=patient.project.name if patient.project else "",
    )

    return render_with_fallback(request, "patient_detail", context)


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
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )

    scan_ids = data.get("scan_ids", [])
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse(
            {"success": False, "error": "scan_ids list is required"}, status=400
        )

    patients = Patient.objects.filter(patient_id__in=scan_ids)
    found_ids = list(patients.values_list("patient_id", flat=True))
    if not found_ids:
        return JsonResponse(
            {"success": False, "error": "No valid scans found to delete"}, status=404
        )

    for patient in patients:
        if not user_is_project_admin(request.user, patient.project):
            return JsonResponse(
                {
                    "success": False,
                    "error": "You do not have permission to permanently delete scans.",
                },
                status=403,
            )
        reasons = annotation_lock_reasons(patient)
        if reasons:
            return JsonResponse(
                {
                    "success": False,
                    "error": f"Patient {patient.patient_id} is locked by existing annotations ({', '.join(reasons)}) and cannot be purged.",
                },
                status=409,
            )

    storage = get_object_storage()
    file_paths = list(
        FileRegistry.objects.filter(urology_patient_id__in=found_ids).values_list(
            "file_path", flat=True
        )
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
def user_profile(request, username=None):
    """The shared profile page, resolved for this namespace.

    The stub this replaces rendered a 35-line template and ignored `username`, so
    profile/<username>/ always showed your own.
    """
    from maxillo.views.profile import user_profile as shared_user_profile

    return shared_user_profile(request, username=username)












@login_required
@require_POST
def add_patients_to_folder(request):
    try:
        data = _json.loads(request.body) if request.body else request.POST
    except _json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )
    scan_ids = data.get("scan_ids", [])
    folder_id = data.get("folder_id")
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse(
            {"success": False, "error": "scan_ids list is required"}, status=400
        )
    if not folder_id or folder_id in ("root", "all"):
        return JsonResponse(
            {"success": False, "error": "A specific folder_id is required"}, status=400
        )
    folder = get_object_or_404(Folder, id=folder_id)
    if not user_is_project_admin(request.user, folder.project):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    patients = Patient.objects.filter(patient_id__in=scan_ids)
    for patient in patients:
        if not user_is_project_admin(request.user, patient.project):
            return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    updated = 0
    for patient in patients:
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
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )
    scan_ids = data.get("scan_ids", [])
    folder_id = data.get("folder_id")
    if not isinstance(scan_ids, list) or not scan_ids:
        return JsonResponse(
            {"success": False, "error": "scan_ids list is required"}, status=400
        )
    if not folder_id or folder_id in ("root", "all"):
        return JsonResponse(
            {"success": False, "error": "A specific folder_id is required"}, status=400
        )
    folder = get_object_or_404(Folder, id=folder_id)
    if not user_is_project_admin(request.user, folder.project):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    patients = Patient.objects.filter(patient_id__in=scan_ids)
    for patient in patients:
        if not user_is_project_admin(request.user, patient.project):
            return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    updated = 0
    for patient in patients:
        if patient.folder_id != folder.id:
            continue
        default_folder = Folder.objects.filter(
            project=patient.project, parent__isnull=True
        ).first()
        patient.folder = default_folder or folder
        patient.save(update_fields=["folder"])
        updated += 1
    return JsonResponse({"success": True, "updated": updated})






def _with_urology_export_mappings(view_func):
    def wrapped(request, *args, **kwargs):
        install_urology_export_mappings()
        return view_func(request, *args, **kwargs)

    return wrapped


def _urology_shared_export_availability(share_token):
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
    project = None
    if project_id:
        project = (
            Project.objects.filter(id=project_id, domain="urology", is_active=True)
            .prefetch_related("modalities", "annotation_methods", "disabled_steps")
            .first()
        )
    if not project:
        project = (
            Project.objects.filter(domain="urology", is_active=True)
            .prefetch_related("modalities", "annotation_methods", "disabled_steps")
            .first()
        )
        if project:
            request.session["current_project_id"] = project.id
    return project






























@login_required
@require_http_methods(["GET", "POST"])
def bulk_upload_patients(request):
    """Bulk upload scans for multiple Urology patients."""
    user_profile = getattr(request.user, "profile", None)
    if not user_profile or not user_profile.can_upload_scans():
        message = "You do not have permission to upload scans."
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": message}, status=403)
        messages.error(request, message)
        return redirect("urology:patient_list")

    project = _current_export_project(request)
    if not project:
        project = Project.objects.filter(domain="urology", is_active=True).first()

    if not project or not user_is_project_admin(request.user, project):
        message = "Bulk upload is restricted to project administrators."
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": message}, status=403)
        messages.error(request, message)
        return redirect("urology:patient_list")

    allowed_modalities = list(
        project.modalities.filter(is_active=True).exclude(slug="rawzip").order_by("name")
    )
    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(project=project).order_by("name"),
        "urology",
    )

    if request.method == "GET":
        return render(
            request,
            "common/upload/bulk_upload.html",
            {
                "current_project": project,
                "folders": folders,
                "allowed_modalities": allowed_modalities,
                "ns": "urology",
                "accept_attribute": ".nii,.nii.gz,.svs,.tiff,.tif",
            },
        )

    uploaded_files = request.FILES.getlist("files")
    if not uploaded_files:
        return _bulk_urology_response(request, [], "Select at least one file to upload.")

    folder = None
    folder_id = request.POST.get("folder")
    if folder_id:
        folder = next((f for f in folders if str(f.id) == str(folder_id)), None)
    if folder is None:
        return _bulk_urology_response(
            request, [], "Choose a folder of this project to upload into."
        )

    forced_modality = None
    forced_slug = (request.POST.get("modality") or "").strip()
    if forced_slug:
        forced_modality = next((m for m in allowed_modalities if m.slug == forced_slug), None)

    image_files = []
    seg_files = []
    for f in uploaded_files:
        fn_lower = (getattr(f, "name", "") or "").lower()
        if fn_lower.endswith(".geojson") or fn_lower.endswith(".json"):
            seg_files.append(f)
        else:
            image_files.append(f)

    results = []
    patient_image_map = {}

    for uploaded_file in image_files:
        res, pat_obj, file_reg, mod_slug = _bulk_upload_one_urology(
            request, project, folder, forced_modality, allowed_modalities, uploaded_file
        )
        results.append(res)
        if pat_obj and file_reg:
            fn = getattr(uploaded_file, "name", "")
            stem = os.path.splitext(fn)[0].lower()
            patient_image_map[stem] = (pat_obj, file_reg, mod_slug)
            # Normalize common prefixes like "copia di "
            if stem.startswith("copia di "):
                patient_image_map[stem[9:].strip()] = (pat_obj, file_reg, mod_slug)

    for seg_file in seg_files:
        fn = getattr(seg_file, "name", "") or "segmentation.geojson"
        raw_stem = os.path.splitext(fn)[0].lower()
        matched = patient_image_map.get(raw_stem)
        if not matched and raw_stem.startswith("copia di "):
            matched = patient_image_map.get(raw_stem[9:].strip())
        if not matched:
            # Fallback: check if any registered stem is contained or vice versa
            for s_stem, s_tuple in patient_image_map.items():
                if s_stem in raw_stem or raw_stem in s_stem:
                    matched = s_tuple
                    break

        if matched:
            pat_obj, parent_file, mod_slug = matched
            try:
                from .file_utils import save_urology_segmentation_file
                seg_reg, _ = save_urology_segmentation_file(pat_obj, mod_slug, seg_file, parent_file=parent_file)
                results.append({
                    "file": fn,
                    "ok": True,
                    "patient_id": pat_obj.patient_id,
                    "patient_name": pat_obj.name,
                    "modality": "segmentation",
                    "note": f"Linked to {parent_file.metadata.get('original_filename', 'slide')}",
                })
            except Exception as s_err:
                logger.warning("Failed linking segmentation %s: %s", fn, s_err)
                results.append({"file": fn, "ok": False, "error": str(s_err)})
        else:
            results.append({"file": fn, "ok": False, "error": "No matching slide found for segmentation"})

    return _bulk_urology_response(request, results)


def _bulk_upload_one_urology(request, project, folder, forced_modality, allowed_modalities, uploaded_file):
    filename = getattr(uploaded_file, "name", "") or "file"
    modality = forced_modality
    if modality is None:
        fn_lower = filename.lower()
        if "confocal" in fn_lower:
            modality = next((m for m in allowed_modalities if "confocal" in m.slug), None)
        if modality is None and any(fn_lower.endswith(ext) for ext in (".svs", ".tiff", ".tif")) :
            modality = next((m for m in allowed_modalities if "wsi" in m.slug), None)
        elif modality is None and any(fn_lower.endswith(ext) for ext in (".nii", ".nii.gz")):
            modality = next((m for m in allowed_modalities if "mri" in m.slug), None)
        if modality is None:
            modality = allowed_modalities[0] if allowed_modalities else None

    if modality is None:
        return {"file": filename, "ok": False, "error": "Could not infer modality for file"}, None, None, None

    try:
        with transaction.atomic():
            base_name = os.path.splitext(filename)[0]
            if base_name.endswith(".nii"):
                base_name = os.path.splitext(base_name)[0]
            clean_name = base_name.replace("_", " ").replace("-", " ").strip().title()
            patient = Patient(
                name=clean_name or "Patient",
                folder=folder,
                uploaded_by=request.user,
            )
            patient.project = project
            patient.save()
            patient.modalities.add(modality)
            file_reg, job = save_urology_modality_file(patient, modality.slug, uploaded_file)
    except Exception as exc:
        logger.warning(f"Bulk upload failed for {filename}: {exc}", exc_info=True)
        return {"file": filename, "ok": False, "error": str(exc)}, None, None, None

    return {
        "file": filename,
        "ok": True,
        "patient_id": patient.patient_id,
        "patient_name": patient.name,
        "modality": modality.slug,
        "job_id": job.id if job else None,
    }, patient, file_reg, modality.slug


def _bulk_urology_response(request, results, error=None):
    is_xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    created = [item for item in results if item.get("ok")]
    failed = [item for item in results if not item.get("ok")]

    if is_xhr:
        if error:
            return JsonResponse({"ok": False, "error": error, "results": results}, status=400)
        return JsonResponse(
            {
                "ok": not failed,
                "created": len(created),
                "failed": len(failed),
                "results": results,
            },
            status=200 if created or not results else 400,
        )

    if error:
        messages.error(request, error)
        return redirect("urology:bulk_upload_patients")
    if created:
        messages.success(request, f"Created {len(created)} patient(s).")
    for item in failed:
        messages.error(request, f"{item['file']}: {item['error']}")
    if created and not failed:
        return redirect("urology:patient_list")
    return redirect("urology:bulk_upload_patients")


@login_required
@require_POST
def add_raw_file(request, patient_id):
    patient = get_patient_for(request.user, Patient, patient_id, "write")

    reasons = annotation_lock_reasons(patient)
    if reasons:
        return JsonResponse({"ok": False, "error": lock_message(reasons)}, status=409)

    modality_slug = request.POST.get("modality") or request.POST.get("modality_slug")
    if not modality_slug:
        return JsonResponse({"ok": False, "error": "Modality is required"}, status=400)

    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return JsonResponse({"ok": False, "error": "No file uploaded"}, status=400)

    try:
        file_reg, job = save_urology_modality_file(patient, modality_slug, uploaded_file)
        modality = Modality.objects.filter(slug=modality_slug).first()
        if modality:
            patient.modalities.add(modality)
        return JsonResponse({
            "ok": True,
            "file": {
                "id": file_reg.id,
                "file_type": file_reg.file_type,
                "file_path": file_reg.file_path,
                "file_size": file_reg.file_size,
            },
        })
    except Exception as e:
        logger.error(f"Error adding raw file: {e}", exc_info=True)
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


@login_required
@require_POST
def delete_raw_file(request, patient_id, file_id):
    patient = get_patient_for(request.user, Patient, patient_id, "write")

    reasons = annotation_lock_reasons(patient)
    if reasons:
        return JsonResponse({"ok": False, "error": lock_message(reasons)}, status=409)

    file_obj = get_object_or_404(patient.files, id=file_id)
    try:
        if file_obj.file_path:
            try:
                get_object_storage().delete(file_obj.file_path)
            except Exception as e:
                logger.warning(f"Failed to delete object {file_obj.file_path}: {e}")
        file_obj.delete()
        return JsonResponse({"ok": True})
    except Exception as e:
        logger.error(f"Error deleting raw file: {e}", exc_info=True)
        return JsonResponse({"ok": False, "error": str(e)}, status=500)
