"""Batch generation of default panoramics for a folder (administrators).

Panoramic reconstruction runs in the browser (NiiVue exposes the native CBCT
voxels, and the arch geometry comes from the CBCT segmentation), so a patient's
default panoramic is produced the first time somebody with edit rights opens it.
That is fine going forward but leaves every already-uploaded patient without one,
and therefore without a panoramic to export.

This page closes that gap: it walks the patients of a folder that have a
completed CBCT but no panoramic, loading each patient view in a hidden frame
where ``cbct_panorex_editor.js`` does its usual unattended pass and posts the
outcome back. Slow, but unattended and using exactly the same code path as a
normal visit — no second implementation of the reconstruction to keep in sync.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from annotations.queries import without_panoramic_arch
from common.models import Project
from common.permissions import filter_folders_for_user, user_is_project_admin

from .domain import get_domain_models, get_namespace
from .helpers import redirect_with_namespace
from .panoramic_state import BROWSER_PANORAMIC_ALGORITHM

logger = logging.getLogger(__name__)

# A batch is bounded so one page cannot queue an unbounded amount of browser work.
WARMUP_BATCH_LIMIT = 200


def _admin_project_or_none(request):
    project_id = request.session.get("current_project_id")
    if not project_id:
        return None
    project = Project.objects.filter(id=project_id).first()
    if project is None or not user_is_project_admin(request.user, project):
        return None
    return project


def _folders_for(request, project):
    Folder = get_domain_models(request)["Folder"]
    return filter_folders_for_user(
        request.user,
        Folder.objects.filter(project=project).order_by("name"),
        get_namespace(request),
    )


def pending_patients(Patient, folder_ids):
    """Patients that can get a default panoramic but do not have one yet.

    A DB-level approximation on purpose: it selects patients whose CBCT processing
    completed and that carry no arch. Whether the completion actually published a
    segmentation is settled in the browser, which already gives up quietly when it did
    not.

    Only an arch from the *current* baker counts as done. One written by a superseded
    algorithm is history, and regenerating it is the whole reason this page exists.

    Both stores are consulted. ``annotations`` holds every arch written from here on;
    ``PanoramicState`` still holds the ones whose conversion has not run, and a patient
    listed from that half would be sent through a silent regeneration it does not need.
    """
    return (
        without_panoramic_arch(
            Patient.objects.filter(
                folder_id__in=folder_ids,
                files__file_type="cbct_processed",
                files__processing_job__status="completed",
            ),
            algorithm_version=BROWSER_PANORAMIC_ALGORITHM,
        )
        .exclude(panoramic_state__algorithm_version=BROWSER_PANORAMIC_ALGORITHM)
        .distinct()
        .order_by("patient_id")
    )


@login_required
def panoramic_warmup(request):
    """Admin page that generates the missing default panoramics of a folder."""
    project = _admin_project_or_none(request)
    if project is None:
        messages.error(
            request, "Generating default panoramics is restricted to project administrators."
        )
        return redirect_with_namespace(request, "patient_list")

    return render(
        request,
        "maxillo/panoramic_warmup.html",
        {
            "project": project,
            "folders": _folders_for(request, project),
            "ns": get_namespace(request),
            "batch_limit": WARMUP_BATCH_LIMIT,
        },
    )


@login_required
@require_GET
def panoramic_warmup_pending(request):
    """JSON list of patients in a folder still missing a default panoramic."""
    project = _admin_project_or_none(request)
    if project is None:
        return JsonResponse({"error": "Permission denied"}, status=403)

    folders = list(_folders_for(request, project))
    try:
        folder_id = int(request.GET.get("folder", ""))
    except (TypeError, ValueError):
        return JsonResponse({"error": "A folder of this project is required."}, status=400)

    folder = next((f for f in folders if f.id == folder_id), None)
    if folder is None:
        return JsonResponse({"error": "A folder of this project is required."}, status=400)

    Patient = get_domain_models(request)["Patient"]
    patients = pending_patients(Patient, [folder.id])
    total = patients.count()
    rows = list(patients[:WARMUP_BATCH_LIMIT].values("patient_id", "name"))

    return JsonResponse(
        {
            "folder": {"id": folder.id, "name": folder.name},
            "total": total,
            "truncated": total > len(rows),
            "patients": [
                {"id": row["patient_id"], "name": row["name"] or f"Patient {row['patient_id']}"}
                for row in rows
            ],
        }
    )
