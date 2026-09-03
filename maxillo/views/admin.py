"""Processing management views (rerun / bulk rerun)."""

from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import JsonResponse
import json
import logging

from .domain import get_domain_models, get_namespace
from common.permissions import user_can_perform_bulk_operations

logger = logging.getLogger(__name__)


@login_required
@require_POST
def rerun_processing(request, patient_id):
    """Set selected existing jobs to pending so workers can pick them up (admin only).

    Accepts JSON body: { "jobs": ["modality_slug1", "modality_slug2", ...] }
    Marks the latest job for each modality as pending and resets processing status.
    """
    try:
        Patient = get_domain_models(request)["Patient"]
        domain = get_namespace(request)
        patient = get_object_or_404(Patient, patient_id=patient_id)
        from common.models import Job
        from ..modality_helpers import get_modality_slugs

        job_filter = {"patient": patient, "domain": "maxillo"}

        try:
            data = json.loads(request.body) if request.body else {}
        except json.JSONDecodeError:
            data = {}

        requested_jobs = data.get("jobs")
        if requested_jobs is None:
            # Default to all modalities if not specified
            requested_jobs = list(get_modality_slugs())

        if not isinstance(requested_jobs, list) or not requested_jobs:
            return JsonResponse(
                {"success": False, "error": "No jobs selected"}, status=400
            )

        # Ensure jobs exist for the requested steps (and their prerequisite
        # closure) so newly-registered steps get a job on existing patients.
        from common.uploads import ensure_step_jobs_for_patient
        created_jobs = ensure_step_jobs_for_patient(patient, requested_jobs)
        created = sorted({job.modality_slug for job in created_jobs})

        updated = []
        not_found = []

        # Process each requested modality dynamically
        for modality_slug in requested_jobs:
            if modality_slug in {"audio", "voice"}:
                not_found.append(modality_slug)
                continue
            # Handle both old-style ProcessingJob and new-style Job
            jobs_found = False

            # Try new Job model first
            try:
                job = (
                    Job.objects.filter(modality_slug=modality_slug, **job_filter)
                    .order_by("-created_at")
                    .first()
                )
                if job:
                    job.status = "pending"
                    job.started_at = None
                    job.completed_at = None
                    job.worker_id = ""
                    job.error_logs = ""
                    job.save()
                    jobs_found = True

                    # Special handling for dependencies
                    if hasattr(job, "update_status_based_on_dependencies"):
                        job.update_status_based_on_dependencies()

                    updated.append(modality_slug)
            except Exception as e:
                logger.error(f"Error processing job for modality {modality_slug}: {e}")

            if not jobs_found and modality_slug not in updated:
                not_found.append(modality_slug)

        msg_parts = []
        if created:
            msg_parts.append(f"Created missing job for: {', '.join(created)}")
        if updated:
            msg_parts.append(f"Updated: {', '.join(updated)}")
        if not_found:
            msg_parts.append(f"No existing job found for: {', '.join(not_found)}")
        message = "; ".join(msg_parts) if msg_parts else "No changes made"

        if updated:
            messages.success(request, f"Reprocessing queued. {message}")
        else:
            messages.warning(request, f"Nothing to rerun. {message}")

        return JsonResponse(
            {
                "success": True,
                "message": message,
                "updated": updated,
                "created": created,
                "not_found": not_found,
            }
        )

    except Exception as e:
        logger.error(f"Error rerunning processing for scan {patient_id}: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_POST
def bulk_rerun_processing(request):
    """Bulk rerun latest existing job per modality per patient (admin only)."""
    try:
        if not user_can_perform_bulk_operations(request.user, request):
            return JsonResponse(
                {"success": False, "error": "You do not have permission to bulk rerun jobs."},
                status=403,
            )

        Patient = get_domain_models(request)["Patient"]
        domain = get_namespace(request)
        from common.models import Job

        try:
            data = json.loads(request.body) if request.body else {}
        except json.JSONDecodeError:
            data = {}

        scan_ids = data.get("scan_ids", [])
        requested_jobs = data.get("jobs", [])

        if not isinstance(scan_ids, list) or not scan_ids:
            return JsonResponse({"success": False, "error": "scan_ids list is required"}, status=400)
        if not isinstance(requested_jobs, list) or not requested_jobs:
            return JsonResponse({"success": False, "error": "jobs list is required"}, status=400)

        valid_scan_ids = []
        for raw_id in scan_ids:
            try:
                valid_scan_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        valid_scan_ids = list(set(valid_scan_ids))

        if not valid_scan_ids:
            return JsonResponse({"success": False, "error": "No valid scan_ids provided"}, status=400)

        patients = Patient.objects.filter(patient_id__in=valid_scan_ids)
        if not patients.exists():
            return JsonResponse({"success": False, "error": "No valid scans found"}, status=404)

        normalized_jobs = []
        for slug in requested_jobs:
            s = str(slug or "").strip()
            if not s:
                continue
            if s not in {"audio", "voice"}:
                normalized_jobs.append(s)
        normalized_jobs = list(set(normalized_jobs))

        if not normalized_jobs:
            return JsonResponse({"success": False, "error": "No valid modalities selected"}, status=400)

        updated_pairs = 0
        not_found_pairs = 0
        updated_by_modality = {}
        not_found_by_modality = {}
        created_slugs = set()

        from common.uploads import ensure_step_jobs_for_patient

        for patient in patients:
            job_filter_base = (
                {"brain_patient": patient, "domain": "brain"}
                if domain == "brain"
                else {"patient": patient, "domain": "maxillo"}
            )

            for modality_slug in normalized_jobs:
                # Ensure the step's job exists (and its prerequisites) before
                # resetting, so newly-registered steps run on existing patients.
                created_slugs.update(
                    job.modality_slug
                    for job in ensure_step_jobs_for_patient(patient, [modality_slug])
                )
                job = (
                    Job.objects.filter(modality_slug=modality_slug, **job_filter_base)
                    .order_by("-created_at")
                    .first()
                )
                if not job:
                    not_found_pairs += 1
                    not_found_by_modality[modality_slug] = not_found_by_modality.get(modality_slug, 0) + 1
                    continue

                job.status = "pending"
                job.started_at = None
                job.completed_at = None
                job.worker_id = ""
                job.error_logs = ""
                job.save()

                if hasattr(job, "update_status_based_on_dependencies"):
                    job.update_status_based_on_dependencies()

                updated_pairs += 1
                updated_by_modality[modality_slug] = updated_by_modality.get(modality_slug, 0) + 1

        message = (
            f"Bulk rerun queued for {updated_pairs} patient-modality pairs "
            f"across {patients.count()} selected scans."
        )

        return JsonResponse(
            {
                "success": True,
                "message": message,
                "selected_scan_count": patients.count(),
                "requested_modalities": normalized_jobs,
                "updated_pairs": updated_pairs,
                "not_found_pairs": not_found_pairs,
                "created_slugs": sorted(created_slugs),
                "updated_by_modality": updated_by_modality,
                "not_found_by_modality": not_found_by_modality,
            }
        )
    except Exception as e:
        logger.error(f"Error in bulk rerun processing: {e}", exc_info=True)
        return JsonResponse({"success": False, "error": str(e)}, status=500)
