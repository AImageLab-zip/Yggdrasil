"""Export views for administrator-only dataset export functionality."""

from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.views import redirect_to_login
from django.contrib import messages
from django.http import JsonResponse, Http404
from django.views.decorators.http import require_POST, require_http_methods
from django.db.models import Q, Count, Sum
from django.urls import reverse
from django.utils import timezone
import os
import json
import logging

from common.models import Modality, FileRegistry
from common.file_access import exists as artifact_exists, streaming_response
from common.object_storage import get_object_storage
from .domain import get_domain_models, get_namespace
from .helpers import redirect_with_namespace

logger = logging.getLogger(__name__)


EXPORT_MODALITY_FILE_TYPES = {
    "cbct": {
        "raw": ["cbct_raw"],
        "processed": ["cbct_processed"],
    },
    "ios": {
        "raw": ["ios_raw_upper", "ios_raw_lower"],
        "processed": ["ios_processed_upper", "ios_processed_lower"],
    },
    "audio": {
        "raw": ["audio_raw"],
        "processed": ["audio_processed"],
    },
    "bite_classification": {
        "raw": [],
        "processed": ["bite_classification"],
    },
    "intraoral": {
        "raw": ["intraoral_raw"],
        "processed": ["intraoral_processed"],
    },
    "intraoral-photo": {
        "raw": ["intraoral_raw"],
        "processed": ["intraoral_processed"],
    },
    "teleradiography": {
        "raw": ["teleradiography_raw"],
        "processed": ["teleradiography_processed"],
    },
    "panoramic": {
        "raw": ["panoramic_raw"],
        "processed": ["panoramic_processed"],
    },
    "braintumor-mri-t1": {
        "raw": ["braintumor_mri_t1_raw"],
        "processed": ["braintumor_mri_t1_processed"],
    },
    "braintumor-mri-t1c": {
        "raw": ["braintumor_mri_t1c_raw"],
        "processed": ["braintumor_mri_t1c_processed"],
    },
    "braintumor-mri-t2": {
        "raw": ["braintumor_mri_t2_raw"],
        "processed": ["braintumor_mri_t2_processed"],
    },
    "braintumor-mri-flair": {
        "raw": ["braintumor_mri_flair_raw"],
        "processed": ["braintumor_mri_flair_processed"],
    },
    "rawzip": {
        "raw": ["generic_raw"],
        "processed": ["generic_processed"],
    },
}


def _coerce_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_content_selection(data, default_when_missing=True):
    has_raw_key = "include_raw" in data
    has_processed_key = "include_processed" in data

    if not has_raw_key and not has_processed_key:
        return (True, True) if default_when_missing else (False, False)

    include_raw = _coerce_bool(data.get("include_raw"), default=False)
    include_processed = _coerce_bool(data.get("include_processed"), default=False)
    return include_raw, include_processed


def _file_type_map_for_selection(include_raw, include_processed):
    file_type_map = {}
    for modality_slug, groups in EXPORT_MODALITY_FILE_TYPES.items():
        file_types = []
        if include_raw:
            file_types.extend(groups.get("raw", []))
        if include_processed:
            file_types.extend(groups.get("processed", []))
        file_type_map[modality_slug] = file_types
    return file_type_map


def _build_shared_download_url(request, share_token):
    """Build absolute shared landing URL for an export token."""
    return request.build_absolute_uri(
        reverse(
            f"{get_namespace(request)}:export_shared_landing",
            kwargs={"share_token": share_token},
        )
    )


def _shared_export_availability(request, share_token):
    """Return export and availability status for shared access."""
    ExportModel = get_domain_models(request)["Export"]
    export = ExportModel.objects.filter(share_token=share_token).first()
    if not export:
        return None, False, "invalid"

    if export.share_mode == "private":
        return export, False, "private"

    if export.status != "completed":
        return export, False, "not_completed"

    if not export.file_path or not artifact_exists(export.file_path):
        return export, False, "missing_file"

    return export, True, ""


def is_admin(user):
    """Check if user is admin (staff or has admin role)."""
    return user.is_staff or user.profile.is_admin()


@login_required
@user_passes_test(is_admin)
def export_list(request):
    """Display export history page with all previous exports."""
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
@user_passes_test(is_admin)
@require_http_methods(["GET", "POST"])
def export_new(request):
    """Create new export page with folder/modality selection."""
    domain_models = get_domain_models(request)
    ExportModel = domain_models["Export"]
    FolderModel = domain_models["Folder"]
    PatientModel = domain_models["Patient"]

    if request.method == "POST":
        # Get form data
        folder_ids = request.POST.getlist("folder_ids")
        modality_slugs = request.POST.getlist("modality_slugs")
        include_raw, include_processed = _resolve_content_selection(
            request.POST, default_when_missing=False
        )

        # Get filters
        filters = {}
        for key in request.POST.keys():
            if key.startswith("filter_"):
                filter_name = key.replace("filter_", "")
                filters[filter_name] = True

        # Validate selections
        if not folder_ids:
            messages.error(request, "Please select at least one folder.")
            return redirect_with_namespace(request, "export_new")

        # Require at least one selection (can be a modality and/or reports only)
        if not modality_slugs:
            messages.error(request, "Please select at least one modality.")
            return redirect_with_namespace(request, "export_new")

        if not include_raw and not include_processed:
            messages.error(
                request,
                "Please select at least one content type: Raw files and/or Processed files.",
            )
            return redirect_with_namespace(request, "export_new")

        # Create export record
        query_params = {
            "folder_ids": [int(fid) for fid in folder_ids],
            "modality_slugs": modality_slugs,
            "filters": filters,
            "include_raw": include_raw,
            "include_processed": include_processed,
        }

        # Generate query summary
        folder_count = len(folder_ids)
        modality_names = []
        include_reports = False
        for slug in modality_slugs:
            if slug == "reports":
                include_reports = True
            else:
                try:
                    modality = Modality.objects.get(slug=slug)
                    modality_names.append(modality.name)
                except Modality.DoesNotExist:
                    modality_names.append(slug)

        # Add Reports to summary if selected
        if include_reports:
            modality_names.append("Reports")

        filter_parts = []
        if filters.get("has_cbct"):
            filter_parts.append("Has CBCT")
        if filters.get("has_ios"):
            filter_parts.append("Has IOS")
        for key, value in filters.items():
            if key.startswith("has_reports_") and value:
                modality_slug = key.replace("has_reports_", "")
                try:
                    modality = Modality.objects.get(slug=modality_slug)
                    filter_parts.append(f"Has Reports for {modality.name}")
                except Modality.DoesNotExist:
                    filter_parts.append(f"Has Reports for {modality_slug}")

        query_summary_parts = [
            f"{folder_count} folder{'s' if folder_count > 1 else ''}"
        ]
        if modality_names:
            query_summary_parts.append(" + ".join(modality_names))
        if filter_parts:
            query_summary_parts.append(", ".join(filter_parts))

        selected_content = []
        if include_raw:
            selected_content.append("Raw")
        if include_processed:
            selected_content.append("Processed")
        query_summary_parts.append(f"Content: {' + '.join(selected_content)}")

        query_summary = ", ".join(query_summary_parts)

        export = ExportModel.objects.create(
            user=request.user,
            status="pending",
            query_params=query_params,
            query_summary=query_summary,
        )

        # Start background processing
        from ..utils.export_processor import start_export_processing

        start_export_processing(export.id, get_namespace(request))

        messages.success(
            request, f"Export #{export.id} created and processing started."
        )
        return redirect_with_namespace(request, "export_list")

    # GET request - show form
    folders = FolderModel.objects.filter(parent__isnull=True).order_by("name")

    # Get patient counts for folders
    folders_with_counts = []
    for folder in folders:
        patient_count = PatientModel.objects.filter(folder=folder).count()
        folders_with_counts.append(
            {
                "folder": folder,
                "patient_count": patient_count,
            }
        )

    # Get all active modalities
    modalities = Modality.objects.filter(is_active=True).order_by("name")

    return render(
        request,
        "maxillo/export_new.html",
        {
            "folders": folders_with_counts,
            "modalities": modalities,
            "ns": get_namespace(request),
        },
    )


@login_required
@user_passes_test(is_admin)
@require_http_methods(["POST", "GET"])
def export_preview(request):
    """AJAX endpoint to get export statistics based on selected criteria."""
    try:
        domain_models = get_domain_models(request)
        PatientModel = domain_models["Patient"]
        VoiceCaptionModel = domain_models["VoiceCaption"]
        domain = get_namespace(request)

        # Get parameters from request
        if request.method == "POST":
            data = json.loads(request.body) if request.body else {}
        else:
            data = request.GET

        folder_ids = data.get("folder_ids", [])
        modality_slugs = data.get("modality_slugs", [])
        filters = data.get("filters", {})
        include_raw, include_processed = _resolve_content_selection(data)
        file_type_map = _file_type_map_for_selection(include_raw, include_processed)

        # Convert to proper types
        if isinstance(folder_ids, str):
            folder_ids = [int(fid) for fid in folder_ids.split(",") if fid]
        else:
            folder_ids = [int(fid) for fid in folder_ids if fid]

        if isinstance(modality_slugs, str):
            modality_slugs = modality_slugs.split(",") if modality_slugs else []

        # Query patients based on folders
        patients = (
            PatientModel.objects.filter(folder_id__in=folder_ids)
            if folder_ids
            else PatientModel.objects.none()
        )

        file_patient_filter = (
            "brain_patient__in" if domain == "brain" else "patient__in"
        )

        # Apply filters (checking for processed files)
        if filters.get("has_cbct"):
            cbct_file_types = file_type_map.get("cbct", [])
            if not cbct_file_types:
                patients = PatientModel.objects.none()
            # Patients with CBCT files for selected content
            cbct_patient_ids = FileRegistry.objects.filter(
                domain=domain, file_type__in=cbct_file_types
            ).values_list(
                "brain_patient_id" if domain == "brain" else "patient_id", flat=True
            )
            cbct_patients = PatientModel.objects.filter(
                patient_id__in=cbct_patient_ids
            ).distinct()
            patients = patients.filter(
                patient_id__in=cbct_patients.values_list("patient_id", flat=True)
            )

        if filters.get("has_ios"):
            ios_file_types = file_type_map.get("ios", [])
            if not ios_file_types:
                patients = PatientModel.objects.none()
            # Patients with IOS files for selected content
            ios_patient_ids = FileRegistry.objects.filter(
                domain=domain,
                file_type__in=ios_file_types,
            ).values_list(
                "brain_patient_id" if domain == "brain" else "patient_id", flat=True
            )
            ios_patients = PatientModel.objects.filter(
                patient_id__in=ios_patient_ids
            ).distinct()
            patients = patients.filter(
                patient_id__in=ios_patients.values_list("patient_id", flat=True)
            )

        # Dynamic modality presence filters
        for key, value in filters.items():
            if key.startswith("has_") and not key.startswith("has_reports_") and value:
                modality_slug = key.replace("has_", "")
                # Map modality slug to file types
                file_types = file_type_map.get(modality_slug, [])
                if file_types:
                    modality_patient_ids = FileRegistry.objects.filter(
                        domain=domain, file_type__in=file_types
                    ).values_list(
                        "brain_patient_id" if domain == "brain" else "patient_id",
                        flat=True,
                    )
                    modality_patients = PatientModel.objects.filter(
                        patient_id__in=modality_patient_ids
                    ).distinct()
                    patients = patients.filter(
                        patient_id__in=modality_patients.values_list(
                            "patient_id", flat=True
                        )
                    )
                else:
                    patients = PatientModel.objects.none()

        # Report presence filters
        for key, value in filters.items():
            if key.startswith("has_reports_") and value:
                modality_slug = key.replace("has_reports_", "")
                # Patients with files for this modality AND voice captions
                try:
                    modality = Modality.objects.get(slug=modality_slug)
                    # Get patients with voice captions for this modality
                    report_patients = (
                        PatientModel.objects.filter(
                            voice_captions__modality=modality,
                            voice_captions__text_caption__isnull=False,
                        )
                        .exclude(voice_captions__text_caption="")
                        .distinct()
                    )
                    patients = patients.filter(
                        patient_id__in=report_patients.values_list(
                            "patient_id", flat=True
                        )
                    )
                except Modality.DoesNotExist:
                    pass

        patient_count = patients.count()
        folder_count = len(folder_ids) if folder_ids else 0
        actual_modality_slugs = (
            [slug for slug in modality_slugs if slug != "reports"]
            if modality_slugs
            else []
        )
        # When reports-only, count as 1 modality for display
        modality_count = (
            len(actual_modality_slugs)
            if actual_modality_slugs
            else (1 if modality_slugs else 0)
        )

        # Calculate file count and size estimate
        # Separate reports from actual modalities
        include_reports = "reports" in modality_slugs
        actual_modality_slugs = [slug for slug in modality_slugs if slug != "reports"]

        if patient_count > 0:
            file_count = 0
            total_size = 0
            if actual_modality_slugs:
                # Get files for selected modalities
                file_types = []
                for slug in actual_modality_slugs:
                    file_types.extend(file_type_map.get(slug, []))
                file_filter = {
                    "domain": domain,
                    file_patient_filter: patients,
                    "file_type__in": file_types,
                }
                files = FileRegistry.objects.filter(**file_filter)
                file_count = files.count()
                total_size = files.aggregate(total=Sum("file_size"))["total"] or 0

            # Add voice caption reports if reports are selected (selected modalities or all when reports-only)
            if include_reports:
                report_modality_slugs = (
                    actual_modality_slugs
                    if actual_modality_slugs
                    else list(
                        Modality.objects.filter(is_active=True).values_list(
                            "slug", flat=True
                        )
                    )
                )
                modality_objects = Modality.objects.filter(
                    slug__in=report_modality_slugs
                )
                voice_captions = VoiceCaptionModel.objects.filter(
                    patient__in=patients,
                    modality__in=modality_objects,
                    text_caption__isnull=False,
                ).exclude(text_caption="")
                voice_caption_count = voice_captions.count()
                file_count += voice_caption_count
                for vc in voice_captions:
                    total_size += len(vc.text_caption.encode("utf-8"))
        else:
            file_count = 0
            total_size = 0

        # Format size
        if total_size < 1024 * 1024:  # Less than 1MB
            size_str = f"~{total_size / 1024:.1f} KB"
        elif total_size < 1024 * 1024 * 1024:  # Less than 1GB
            size_str = f"~{total_size / (1024 * 1024):.1f} MB"
        else:
            size_str = f"~{total_size / (1024 * 1024 * 1024):.2f} GB"

        return JsonResponse(
            {
                "success": True,
                "patient_count": patient_count,
                "folder_count": folder_count,
                "modality_count": modality_count,
                "file_count": file_count,
                "estimated_size": size_str,
                "estimated_size_bytes": total_size,
            }
        )

    except Exception as e:
        logger.error(f"Error in export_preview: {e}", exc_info=True)
        return JsonResponse(
            {
                "success": False,
                "error": str(e),
            },
            status=500,
        )


def _recover_stuck_export(export):
    """
    If export is stuck in 'processing' but a completed ZIP exists in object
    storage (process died before DB update), mark it as completed.
    """
    if export.status != "processing":
        return export
    try:
        storage = get_object_storage()

        if export.file_path and artifact_exists(export.file_path):
            info = storage.head(export.file_path)
            size = int(info.content_length or 0)
            export.mark_completed(file_path=export.file_path, file_size=size)
            export.refresh_from_db()
            logger.info(
                "Recovered stuck export %s: marked completed from key %s",
                export.id,
                export.file_path,
            )
            return export

        prefix = f"exports/export_{export.id}_"
        candidates = [
            key
            for key in storage.list_keys(prefix)
            if key.startswith(prefix) and key.endswith(".zip")
        ]
        if not candidates:
            return export

        key = sorted(candidates)[-1]
        info = storage.head(key)
        size = int(info.content_length or 0)
        export.mark_completed(file_path=key, file_size=size)
        export.refresh_from_db()
        logger.info(
            "Recovered stuck export %s: marked completed from key %s",
            export.id,
            key,
        )
    except Exception as e:
        logger.warning(f"Could not recover export {export.id}: {e}")
    return export


@login_required
@user_passes_test(is_admin)
def export_status(request, export_id):
    """AJAX endpoint to get current export status."""
    export = get_object_or_404(get_domain_models(request)["Export"], id=export_id)

    # Check permissions
    if export.user != request.user and not request.user.is_staff:
        return JsonResponse({"error": "Permission denied"}, status=403)

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
@user_passes_test(is_admin)
def export_download(request, export_id):
    """Download export ZIP file."""
    export = get_object_or_404(get_domain_models(request)["Export"], id=export_id)

    # Check permissions
    if export.user != request.user and not request.user.is_staff:
        messages.error(request, "You do not have permission to download this export.")
        return redirect_with_namespace(request, "export_list")

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
@user_passes_test(is_admin)
@require_POST
def export_share_update(request, export_id):
    """Update share settings for a completed export."""
    export = get_object_or_404(get_domain_models(request)["Export"], id=export_id)

    if export.user != request.user and not request.user.is_staff:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

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
        update_fields.extend(["share_token", "shared_at"])
        export.save(update_fields=update_fields)
        return JsonResponse(
            {
                "success": True,
                "share_mode": export.share_mode,
                "share_url": None,
            }
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
    """Render shared export landing page with availability details."""
    export, is_available, _reason = _shared_export_availability(request, share_token)

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
            "share_token": share_token,
            "file_size_human": format_file_size(export.file_size)
            if export and export.file_size
            else None,
        },
    )


@require_http_methods(["GET"])
def export_shared_download(request, share_token):
    """Download export ZIP using a share token."""
    export, is_available, _reason = _shared_export_availability(request, share_token)

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
@user_passes_test(is_admin)
@require_POST
def export_delete(request, export_id):
    """Delete export record and optionally the ZIP file."""
    export = get_object_or_404(get_domain_models(request)["Export"], id=export_id)

    # Check permissions
    if export.user != request.user and not request.user.is_staff:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        # Optionally delete file
        if export.file_path:
            try:
                get_object_storage().delete(export.file_path)
            except Exception as e:
                logger.warning(f"Could not delete export file {export.file_path}: {e}")

        # Delete export record
        export.delete()

        return JsonResponse({"success": True})

    except Exception as e:
        logger.error(f"Error deleting export: {e}", exc_info=True)
        return JsonResponse({"success": False, "error": str(e)}, status=500)


def format_file_size(size_bytes):
    """Format file size in human-readable format."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
