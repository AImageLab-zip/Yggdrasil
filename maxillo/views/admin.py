"""Processing management views (rerun / bulk rerun).

The queueing itself lives in ``common.rerun`` so every domain shares one
implementation; these views own the permission check and the response shape.
"""

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import JsonResponse
import json
import logging

from .domain import get_domain_models
from common.permissions import get_patient_for, user_is_patient_admin
from common.rerun import bulk_rerun_steps, describe, rerun_steps_for_patient

logger = logging.getLogger(__name__)


def _requested_jobs(request):
    """The ``jobs`` list from a JSON body, or ``None`` when absent/unparseable."""
    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        data = {}
    return data


@login_required
@require_POST
def rerun_processing(request, patient_id):
    """Queue the selected processing steps for one patient (admin only).

    Accepts JSON body: ``{"jobs": ["step-slug", ...]}``. A step that has never
    run for this patient is created and queued, not skipped -- see
    ``common.rerun``.
    """
    Patient = get_domain_models(request)["Patient"]
    patient = get_patient_for(request.user, Patient, patient_id, "admin")
    try:
        data = _requested_jobs(request)
        requested_jobs = data.get("jobs")
        if requested_jobs is None:
            # Default to every step the patient's inputs make possible.
            from ..modality_helpers import get_modality_slugs

            requested_jobs = list(get_modality_slugs())

        if not isinstance(requested_jobs, list) or not requested_jobs:
            return JsonResponse({"success": False, "error": "No jobs selected"}, status=400)

        result = rerun_steps_for_patient(patient, requested_jobs)
        message = describe(result)

        if result["updated"]:
            messages.success(request, f"Reprocessing queued. {message}")
        else:
            messages.warning(request, f"Nothing to rerun. {message}")

        return JsonResponse(
            {
                "success": True,
                "message": message,
                "updated": result["updated"],
                "created": result["created"],
                "not_found": result["not_found"],
            }
        )

    except Exception as e:
        logger.error(f"Error rerunning processing for scan {patient_id}: {e}", exc_info=True)
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_POST
def bulk_rerun_processing(request):
    """Queue the selected processing steps across several patients (admin only)."""
    try:
        Patient = get_domain_models(request)["Patient"]
        data = _requested_jobs(request)
        scan_ids = data.get("scan_ids", [])
        requested_jobs = data.get("jobs", [])

        if not isinstance(scan_ids, list) or not scan_ids:
            return JsonResponse({"success": False, "error": "scan_ids list is required"}, status=400)
        if not isinstance(requested_jobs, list) or not requested_jobs:
            return JsonResponse({"success": False, "error": "jobs list is required"}, status=400)

        valid_scan_ids = set()
        for raw_id in scan_ids:
            try:
                valid_scan_ids.add(int(raw_id))
            except (TypeError, ValueError):
                continue

        if not valid_scan_ids:
            return JsonResponse({"success": False, "error": "No valid scan_ids provided"}, status=400)

        # Each patient is authorized against its own project; ids the user does
        # not administer are dropped exactly like ids that do not exist.
        patients = [
            patient
            for patient in Patient.objects.select_related("project").filter(
                patient_id__in=valid_scan_ids
            )
            if user_is_patient_admin(request.user, patient)
        ]
        if not patients:
            return JsonResponse({"success": False, "error": "No valid scans found"}, status=404)

        result = bulk_rerun_steps(patients, requested_jobs)
        if not result["requested"]:
            return JsonResponse({"success": False, "error": "No valid modalities selected"}, status=400)

        return JsonResponse(
            {
                "success": True,
                "message": (
                    f"Bulk rerun queued for {result['updated_pairs']} patient-modality pairs "
                    f"across {len(patients)} selected scans."
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
    except Exception as e:
        logger.error(f"Error in bulk rerun processing: {e}", exc_info=True)
        return JsonResponse({"success": False, "error": str(e)}, status=500)
