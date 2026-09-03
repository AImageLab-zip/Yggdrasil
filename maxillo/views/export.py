"""Export views: build, run, share and download dataset exports.

The export builder is project-scoped: folders, selectable artifacts and filters
all come from the project selected in the sidebar (see
``common.export_catalog``), so an annotator on a CBCT project is never shown MRI
channels or IOS filters.
"""

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.contrib import messages
from django.http import JsonResponse, Http404, HttpResponseGone
from django.views.decorators.http import require_POST, require_http_methods
from django.db import OperationalError
from django.db.models import Sum
from django.utils import timezone
import os
import json
import logging
import time

from common import export_catalog, export_ui
from common.models import FileRegistry, Project
from common.export_share import is_share_expired, resolve_share_expiry
from common.file_access import exists as artifact_exists, streaming_response
from common.object_storage import get_object_storage
from common.permissions import filter_folders_for_user, user_can_create_export, user_is_project_admin
from .domain import get_domain_models, get_namespace
from .helpers import redirect_with_namespace
from common.export_processing import (
    build_shared_download_url as _build_shared_download_url,
    format_file_size,
    kill_export_processes as _kill_export_processes,
    recover_stuck_export as _recover_stuck_export,
)

logger = logging.getLogger(__name__)


def _current_project(request):
    """Project the export builder is scoped to (the one selected in the sidebar).

    Exports used to list every folder of the domain regardless of project, so a
    staff user picking "General" could not tell which project it belonged to.
    """
    project_id = request.session.get("current_project_id")
    if not project_id:
        return None
    return (
        Project.objects.filter(id=project_id)
        .prefetch_related("modalities", "annotation_methods", "disabled_steps")
        .first()
    )


def _shared_export_availability(request, share_token):
    """Return export and availability status for shared access."""
    ExportModel = get_domain_models(request)["Export"]
    export = ExportModel.objects.filter(share_token=share_token).first()
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


def is_admin(user):
    """Whether the user is a global administrator (staff or admin role).

    Deliberately *not* the export gate: exports are project-scoped, so the
    per-export views use ``_require_own_export`` (project export rights + owner)
    instead. This stays only for the share-expiry "never expires" privilege.
    """
    return user.is_staff or user.profile.is_admin()


def _require_own_export(request, export_id, *, json_response=False):
    """Resolve an export the caller may act on, or an error response.

    Returns ``(export, None)`` on success and ``(None, response)`` otherwise.
    The per-export views used to be gated on global staff/admin while
    ``export_new``/``export_list`` used the project-scoped ``_can_use_exports``,
    so a project administrator could create an export and then be refused its own
    download.
    """
    def deny(message, status):
        if json_response:
            return JsonResponse({"success": False, "error": message}, status=status)
        messages.error(request, message)
        return redirect_with_namespace(request, "export_list")

    if not _can_use_exports(request):
        return None, deny("You do not have permission to access exports.", 403)

    ExportModel = get_domain_models(request)["Export"]
    export = ExportModel.objects.filter(id=export_id).first()
    if export is None:
        if json_response:
            return None, JsonResponse({"success": False, "error": "Export not found."}, status=404)
        raise Http404("Export not found.")

    if export.user_id != request.user.id and not request.user.is_staff:
        return None, deny("You do not have permission to access this export.", 403)
    return export, None


def _laparoscopy_export_query_summary(folder_count):
    return ", ".join(
        [
            f"{folder_count} folder{'s' if folder_count != 1 else ''}",
            "Laparoscopy subsampled videos",
            "Per-frame multilayer NPZ masks",
            "All subsampled frames",
        ]
    )


def _laparoscopy_export_new(request, ExportModel):
    from laparoscopy.export_processor import get_laparoscopy_export_folders

    if request.method == "POST":
        folder_ids = request.POST.getlist("folder_ids")
        if not folder_ids:
            messages.error(request, "Please select at least one folder.")
            return redirect_with_namespace(request, "export_new")

        query_params = {
            "domain": "laparoscopy",
            "export_variant": "video_masks_v1",
            "folder_ids": folder_ids,
            "mask_format": "npz_multilayer",
            "include_all_frames": True,
            "video_subtype": "subsampled",
        }
        export = ExportModel.objects.create(
            user=request.user,
            status="pending",
            query_params=query_params,
            query_summary=_laparoscopy_export_query_summary(len(folder_ids)),
        )

        from common.export_processing import start_export_processing

        start_export_processing(export.id, "laparoscopy")
        messages.success(request, f"Export #{export.id} created and processing started.")
        return redirect_with_namespace(request, "export_list")

    return render(
        request,
        "laparoscopy/export_new.html",
        {
            "folders": get_laparoscopy_export_folders(),
            "ns": get_namespace(request),
        },
    )


def _laparoscopy_export_preview(folder_ids):
    from laparoscopy.export_processor import build_laparoscopy_export_preview

    preview = build_laparoscopy_export_preview(folder_ids)
    size_bytes = int(preview["estimated_size_bytes"] or 0)
    return JsonResponse(
        {
            "success": True,
            "patient_count": preview["patient_count"],
            "folder_count": len(folder_ids),
            "exportable_patient_count": preview["exportable_patient_count"],
            "file_count": preview["file_count"],
            "estimated_size": format_file_size(size_bytes),
            "estimated_size_bytes": size_bytes,
        }
    )
def _can_use_exports(request):
    """Whether the user may use exports at all (a coarse gate).

    Per-folder rights are still checked when an export is created. Folders nest
    now, so this considers every folder rather than only the roots -- a user whose
    access is to a sub-folder was previously refused outright -- and narrows to
    the selected project when there is one.
    """
    if user_is_project_admin(request.user, request):
        return True
    FolderModel = get_domain_models(request)["Folder"]
    folders = FolderModel.objects.all()
    project_id = request.session.get("current_project_id")
    if project_id:
        folders = folders.filter(project_id=project_id)
    for folder in folders.only("id", "project"):
        if user_can_create_export(request.user, folder, request):
            return True
    return False


@login_required
def export_list(request):
    """Display export history page with all previous exports."""
    if not _can_use_exports(request):
        messages.error(request, "You do not have permission to access exports.")
        return redirect_with_namespace(request, "patient_list")
    ExportModel = get_domain_models(request)["Export"]
    exports = ExportModel.objects.filter(user=request.user).order_by("-created_at")

    # Format file sizes for display
    exports_with_sizes = []
    for export in exports:
        if export.file_size:
            if export.file_size < 1024:
                size_display = f"{export.file_size} B"
            elif export.file_size < 1048576:
                size_display = f"{export.file_size / 1024:.1f} KB"
            elif export.file_size < 1073741824:
                size_display = f"{export.file_size / 1048576:.1f} MB"
            else:
                size_display = f"{export.file_size / 1073741824:.2f} GB"
        else:
            size_display = None
        exports_with_sizes.append(
            {
                "export": export,
                "size_display": size_display,
            }
        )

    # Pagination if needed
    from django.core.paginator import Paginator

    paginator = Paginator(exports_with_sizes, 50)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "maxillo/export_list.html",
        {
            "exports": page_obj,
            "page_obj": page_obj,
            "ns": get_namespace(request),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def export_new(request):
    """Create new export page: project folders, artifacts, and project filters."""
    if not _can_use_exports(request):
        messages.error(request, "You do not have permission to create exports.")
        return redirect_with_namespace(request, "patient_list")
    domain_models = get_domain_models(request)
    ExportModel = domain_models["Export"]
    if get_namespace(request) == "laparoscopy":
        return _laparoscopy_export_new(request, ExportModel)

    FolderModel = domain_models["Folder"]
    PatientModel = domain_models["Patient"]
    domain = get_namespace(request)
    project = _current_project(request)

    if project is None:
        messages.error(request, "Select a project before creating an export.")
        return redirect_with_namespace(request, "patient_list")

    if request.method == "POST":
        try:
            folder_ids = sorted({int(fid) for fid in request.POST.getlist("folder_ids")})
        except (TypeError, ValueError):
            messages.error(request, "Invalid folder selection.")
            return redirect_with_namespace(request, "export_new")
        artifact_keys = request.POST.getlist("artifacts")
        filters = export_catalog.filters_from_form(request.POST)

        if not folder_ids:
            messages.error(request, "Please select at least one folder.")
            return redirect_with_namespace(request, "export_new")

        selected_folders = FolderModel.objects.filter(id__in=folder_ids, project=project)
        if selected_folders.count() != len(folder_ids):
            messages.error(request, "Select folders from the current project only.")
            return redirect_with_namespace(request, "export_new")
        for folder in selected_folders:
            if not user_can_create_export(request.user, folder, request):
                messages.error(request, "You do not have permission to export from selected folders.")
                return redirect_with_namespace(request, "export_new")

        # Artifacts are validated against the project: a client cannot POST an
        # artifact for a modality the project does not enable.
        allowed_keys = export_ui.allowed_artifact_keys(domain, project)
        artifact_keys = [key for key in artifact_keys if key in allowed_keys]
        if not artifact_keys:
            messages.error(request, "Please select at least one artifact to export.")
            return redirect_with_namespace(request, "export_new")

        artifacts = export_catalog.resolve_artifacts(domain, artifact_keys)
        query_params = {
            "domain": domain,
            "project_id": project.id,
            "folder_ids": [int(fid) for fid in folder_ids],
            "artifacts": artifact_keys,
            "filters": filters,
            # Kept for tools that read query_params expecting the old shape.
            "modality_slugs": sorted(export_catalog.modality_slugs_for(artifacts)),
        }

        folder_count = len(folder_ids)
        summary_parts = [f"{folder_count} folder{'s' if folder_count != 1 else ''}"]
        summary_parts.append(
            ", ".join(artifact.label for artifact in artifacts) or "nothing"
        )
        described = export_catalog.describe_filters(
            domain, project, [m.slug for m in export_ui.project_modalities(project)], filters
        )
        if described:
            summary_parts.append(", ".join(described))

        export = ExportModel.objects.create(
            user=request.user,
            status="pending",
            query_params=query_params,
            query_summary=", ".join(summary_parts),
        )

        from common.export_processing import start_export_processing

        start_export_processing(export.id, domain)
        messages.success(request, f"Export #{export.id} created and processing started.")
        return redirect_with_namespace(request, "export_list")

    # GET: build the form from the project.
    folders = export_ui.folder_tree(
        filter_folders_for_user(
            request.user,
            FolderModel.objects.filter(project=project).order_by("name"),
            domain,
        ),
        PatientModel,
        domain,
    )
    visible_folder_ids = [entry["folder"].id for entry in folders]
    patients_in_scope = PatientModel.objects.filter(folder_id__in=visible_folder_ids)
    modalities = export_ui.project_modalities(project)

    # Panoramics are reconstructed in the browser, so already-uploaded patients
    # may have none to export yet. Point administrators at the batch page rather
    # than leaving an unexplained zero next to the panoramic artifacts.
    warmup_url = None
    if domain == "maxillo" and any(m.slug == "cbct" for m in modalities):
        if user_is_project_admin(request.user, project):
            from django.urls import NoReverseMatch, reverse

            try:
                warmup_url = reverse(f"{domain}:panoramic_warmup")
            except NoReverseMatch:
                warmup_url = None

    return render(
        request,
        "maxillo/export_new.html",
        {
            "project": project,
            "folders": folders,
            "modalities": modalities,
            "panoramic_warmup_url": warmup_url,
            "artifact_groups": export_ui.artifact_groups(
                domain, project, patients_in_scope
            ),
            "filter_groups": export_ui.grouped_filters(
                domain, project, [m.slug for m in modalities]
            ),
            "ns": get_namespace(request),
        },
    )


@login_required
@require_http_methods(["POST", "GET"])
def export_preview(request):
    """AJAX endpoint: patient / file / size counts for the current selection.

    Uses the same folder closure, artifact resolution and filter application as
    the real export run, so the preview can never disagree with the ZIP.
    """
    if not _can_use_exports(request):
        return JsonResponse({"error": "Permission denied"}, status=403)
    try:
        domain_models = get_domain_models(request)
        PatientModel = domain_models["Patient"]
        FolderModel = domain_models["Folder"]
        domain = get_namespace(request)

        data = (json.loads(request.body) if request.body else {}) if request.method == "POST" else request.GET

        folder_ids = data.get("folder_ids", [])
        if isinstance(folder_ids, str):
            folder_ids = [fid for fid in folder_ids.split(",") if fid]
        folder_ids = [int(fid) for fid in folder_ids if str(fid).strip()]

        if domain == "laparoscopy":
            return _laparoscopy_export_preview(folder_ids)

        artifact_keys = data.get("artifacts", [])
        if isinstance(artifact_keys, str):
            artifact_keys = [key for key in artifact_keys.split(",") if key]
        artifacts = export_catalog.resolve_artifacts(domain, artifact_keys)
        filters = export_catalog.normalize_filters(data.get("filters", {}))

        project = _current_project(request)
        patients = _preview_patients(
            PatientModel, FolderModel, domain, project, folder_ids, filters, artifacts
        )

        patient_count = patients.count()
        file_count = 0
        total_size = 0
        if patient_count and artifacts:
            file_count, total_size = _preview_totals(domain, patients, artifacts)

        if total_size < 1024 * 1024:
            size_str = f"~{total_size / 1024:.1f} KB"
        elif total_size < 1024 * 1024 * 1024:
            size_str = f"~{total_size / (1024 * 1024):.1f} MB"
        else:
            size_str = f"~{total_size / (1024 * 1024 * 1024):.2f} GB"

        return JsonResponse(
            {
                "success": True,
                "patient_count": patient_count,
                "folder_count": len(folder_ids),
                "modality_count": len(export_catalog.modality_slugs_for(artifacts)),
                "artifact_count": len(artifacts),
                "file_count": file_count,
                "estimated_size": size_str,
                "estimated_size_bytes": total_size,
            }
        )

    except Exception as e:
        logger.error(f"Error in export_preview: {e}", exc_info=True)
        return JsonResponse({"success": False, "error": str(e)}, status=500)


def _preview_patients(
    PatientModel, FolderModel, domain, project, folder_ids, filters, artifacts
):
    """Patient queryset for a preview: same folder closure + filters as the run."""
    if not folder_ids:
        return PatientModel.objects.none()

    # Folder selection includes sub-folders, exactly as the export does.
    closure = set(folder_ids)
    frontier = list(folder_ids)
    while frontier:
        children = list(
            FolderModel.objects.filter(parent_id__in=frontier)
            .exclude(id__in=closure)
            .values_list("id", flat=True)
        )
        if not children:
            break
        closure.update(children)
        frontier = children

    patients = PatientModel.objects.filter(folder_id__in=sorted(closure))
    if project is not None:
        patients = patients.filter(project=project)
    return export_catalog.apply_filters(patients, domain, filters, artifacts=artifacts)


def _preview_totals(domain, patients, artifacts):
    """(file count, byte total) for the selected artifacts across these patients.

    File-backed artifacts aggregate over FileRegistry. Database-backed ones are
    counted per patient with a flat per-document estimate: the real serialization
    is small and roughly constant, and this endpoint runs on every keystroke.
    """
    file_count = 0
    total_size = 0
    file_artifacts = [a for a in artifacts if a.is_file_backed]
    if file_artifacts:
        query = None
        for artifact in file_artifacts:
            query = artifact.registry_q() if query is None else query | artifact.registry_q()
        rows = FileRegistry.objects.filter(
            domain=domain, patient__in=patients
        ).filter(query)
        file_count += rows.count()
        total_size += rows.aggregate(total=Sum("file_size"))["total"] or 0

    for artifact in artifacts:
        if artifact.collector == "captions":
            captions = _caption_queryset(domain, patients, artifacts)
            count = captions.count()
            file_count += count
            total_size += sum(
                len(text.encode("utf-8"))
                for text in captions.values_list("text_caption", flat=True)
            )
        elif artifact.collector == "occlusion" and domain == "maxillo":
            from maxillo.models import Classification

            count = (
                Classification.objects.filter(patient__in=patients)
                .values_list("patient_id", flat=True)
                .distinct()
                .count()
            )
            file_count += count
            total_size += count * 450
        elif artifact.collector == "tooth_segmentation" and domain == "maxillo":
            # One document per photograph that still has polygons on the latest revision,
            # which is exactly what `_collect_tooth_segmentation` yields. Counted from
            # `annotations/` because that is where the polygons now live;
            # `IntraoralToothSegmentation` stops moving the moment anybody edits a study.
            from annotations.queries import tooth_segmentation_image_count

            count = tooth_segmentation_image_count(patients)
            file_count += count
            total_size += count * 2048

    return file_count, total_size


def _caption_queryset(domain, patients, artifacts):
    from brain.models import VoiceCaption as BrainVoiceCaption
    from maxillo.models import VoiceCaption as MaxilloVoiceCaption

    model = BrainVoiceCaption if domain == "brain" else MaxilloVoiceCaption
    captions = model.objects.filter(
        patient__in=patients, text_caption__isnull=False
    ).exclude(text_caption="")
    modality_slugs = export_catalog.modality_slugs_for(artifacts)
    if modality_slugs:
        captions = captions.filter(modality__in=sorted(modality_slugs))
    return captions


@login_required
def export_status(request, export_id):
    """AJAX endpoint to get current export status."""
    export, denied = _require_own_export(request, export_id, json_response=True)
    if denied is not None:
        return denied

    # Recover stuck exports: if still "processing" but ZIP exists and is old, mark completed
    export = _recover_stuck_export(export)

    response_data = {
        "id": export.id,
        "status": export.status,
        "query_summary": export.query_summary,
    }

    if export.status == "completed":
        response_data["file_size"] = export.file_size
        response_data["file_size_human"] = format_file_size(export.file_size)
        response_data["patient_count"] = export.patient_count
        if export.completed_at:
            response_data["completed_at"] = export.completed_at.isoformat()

    if export.status == "failed":
        response_data["error_message"] = export.error_message

    if export.status == "processing":
        if export.started_at:
            response_data["started_at"] = export.started_at.isoformat()
        if export.patient_count:
            response_data["patient_count"] = export.patient_count
        if getattr(export, "progress_message", None):
            response_data["progress_message"] = export.progress_message
        if getattr(export, "progress_percent", None) is not None:
            response_data["progress_percent"] = export.progress_percent

    return JsonResponse(response_data)


@login_required
def export_download(request, export_id):
    """Download export ZIP file."""
    export, denied = _require_own_export(request, export_id)
    if denied is not None:
        return denied

    # Check status
    if export.status != "completed":
        messages.error(request, "Export is not yet completed.")
        return redirect_with_namespace(request, "export_list")

    # Check file exists
    if not export.file_path or not artifact_exists(export.file_path):
        messages.error(request, "Export file not found.")
        export.mark_failed("Export file not found in storage")
        return redirect_with_namespace(request, "export_list")

    # Serve file
    try:
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
    except Exception as e:
        logger.error(f"Error serving export file: {e}", exc_info=True)
        messages.error(request, "Error serving export file.")
        return redirect_with_namespace(request, "export_list")


@login_required
@require_POST
def export_share_update(request, export_id):
    """Update share settings for a completed export."""
    export, denied = _require_own_export(request, export_id, json_response=True)
    if denied is not None:
        return denied

    if export.status != "completed":
        return JsonResponse(
            {"success": False, "error": "Only completed exports can be shared"},
            status=400,
        )

    try:
        data = json.loads(request.body) if request.body else request.POST
    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )

    share_mode = data.get("share_mode", "").strip()
    if share_mode not in ["private", "authenticated", "public"]:
        return JsonResponse(
            {"success": False, "error": "Invalid share mode"}, status=400
        )

    regenerate_raw = data.get("regenerate", False)
    regenerate = (
        str(regenerate_raw).lower() in ["1", "true", "yes"]
        if not isinstance(regenerate_raw, bool)
        else regenerate_raw
    )

    export.share_mode = share_mode
    update_fields = ["share_mode"]

    if share_mode == "private":
        export.share_token = None
        export.shared_at = None
        export.expires_at = None
        update_fields.extend(["share_token", "shared_at", "expires_at"])
        export.save(update_fields=update_fields)
        return JsonResponse(
            {
                "success": True,
                "share_mode": export.share_mode,
                "share_url": None,
                "expires_at": None,
            }
        )

    # This endpoint is already gated on staff/project-admin (is_admin).
    expires_at, expiry_error = resolve_share_expiry(
        data.get("expires_in_days"),
        current=export.expires_at,
        can_set_never=is_admin(request.user),
    )
    if expiry_error:
        return JsonResponse({"success": False, "error": expiry_error}, status=400)

    if regenerate or not export.share_token:
        export.ensure_share_token(force_new=regenerate)

    export.shared_at = timezone.now()
    export.expires_at = expires_at
    export.save(update_fields=["share_mode", "shared_at", "expires_at"])

    return JsonResponse(
        {
            "success": True,
            "share_mode": export.share_mode,
            "share_url": _build_shared_download_url(request, export.share_token),
            "expires_at": export.expires_at.isoformat() if export.expires_at else None,
        }
    )


@require_http_methods(["GET"])
def export_shared_landing(request, share_token):
    """Render shared export landing page with availability details."""
    export, is_available, reason = _shared_export_availability(request, share_token)

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
            "ns": get_namespace(request),
            "export": export,
            "is_available": is_available,
            "is_expired": reason == "expired",
            "share_token": share_token,
            "file_size_human": format_file_size(export.file_size)
            if export and export.file_size
            else None,
        },
        status=410 if reason == "expired" else 200,
    )


@require_http_methods(["GET"])
def export_shared_download(request, share_token):
    """Download export ZIP using a share token."""
    export, is_available, reason = _shared_export_availability(request, share_token)

    if reason == "expired":
        return HttpResponseGone("This share link has expired.")

    if not export or not is_available:
        raise Http404("Export is not available.")

    if export.share_mode == "authenticated" and not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())

    try:
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
    except Exception as e:
        logger.error(f"Error serving shared export file: {e}", exc_info=True)
        raise Http404("Error serving export file.")


@login_required
@require_POST
def export_delete(request, export_id):
    """Delete export record and optionally the ZIP file."""
    export, denied = _require_own_export(request, export_id, json_response=True)
    if denied is not None:
        return denied

    file_path = export.file_path

    # Delete DB row first (fast), retry on transient lock waits.
    deleted = False
    max_attempts = 4
    for attempt in range(max_attempts):
        try:
            deleted_count, _ = get_domain_models(request)["Export"].objects.filter(
                id=export_id
            ).delete()
            deleted = deleted_count > 0
            break
        except OperationalError as e:
            msg = str(e)
            is_lock_timeout = "1205" in msg or "Lock wait timeout exceeded" in msg
            if not is_lock_timeout or attempt == max_attempts - 1:
                logger.error(f"Error deleting export {export_id}: {e}", exc_info=True)
                return JsonResponse(
                    {
                        "success": False,
                        "error": "Database is busy. Please retry in a few seconds.",
                    },
                    status=409,
                )
            time.sleep(0.4 * (attempt + 1))
        except Exception as e:
            logger.error(f"Error deleting export {export_id}: {e}", exc_info=True)
            return JsonResponse({"success": False, "error": str(e)}, status=500)

    if not deleted:
        return JsonResponse(
            {"success": False, "error": "Export not found or already deleted."},
            status=404,
        )

    # Best effort object cleanup after DB deletion.
    if file_path:
        try:
            get_object_storage().delete(file_path)
        except Exception as e:
            logger.warning(f"Could not delete export file {file_path}: {e}")

    return JsonResponse({"success": True})


@login_required
@require_POST
def export_stop(request, export_id):
    """Manually stop a processing export, kill worker, and delete partial ZIPs."""
    export, denied = _require_own_export(request, export_id, json_response=True)
    if denied is not None:
        return denied

    if export.status not in {"processing", "pending"}:
        return JsonResponse(
            {
                "success": False,
                "error": f"Export is not running (status: {export.status}).",
            },
            status=409,
        )

    killed_pids = _kill_export_processes(export.id)

    deleted_keys = []
    storage_warnings = []
    storage = get_object_storage()

    # Delete known key if already set
    if export.file_path:
        try:
            storage.delete(export.file_path)
            deleted_keys.append(export.file_path)
        except Exception as e:
            storage_warnings.append(f"Could not delete {export.file_path}: {e}")

    # Delete partial/final keys for this export id
    prefix = f"exports/export_{export.id}_"
    try:
        for key in storage.list_keys(prefix):
            if not key.startswith(prefix) or not key.endswith(".zip"):
                continue
            try:
                storage.delete(key)
                deleted_keys.append(key)
            except Exception as e:
                storage_warnings.append(f"Could not delete {key}: {e}")
    except Exception as e:
        storage_warnings.append(f"Could not list keys for prefix {prefix}: {e}")

    stopped_at = timezone.now().strftime("%Y-%m-%d %H:%M:%S %Z")
    who = getattr(request.user, "username", "unknown")
    message = f"Stopped manually by {who} at {stopped_at}."
    if killed_pids:
        message += f" Killed worker PID(s): {', '.join(str(p) for p in killed_pids)}."
    if deleted_keys:
        message += f" Deleted {len(set(deleted_keys))} ZIP object(s)."
    if storage_warnings:
        message += " Storage cleanup had warnings."

    export.mark_failed(message)

    return JsonResponse(
        {
            "success": True,
            "killed_pids": killed_pids,
            "deleted_keys": sorted(set(deleted_keys)),
            "warnings": storage_warnings,
            "status": "failed",
            "error_message": message,
        }
    )


