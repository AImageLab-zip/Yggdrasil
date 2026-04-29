"""Admin control panel and processing management views."""

from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.db.models import Count, Q
import json
import logging

from .domain import get_domain_models, get_namespace

logger = logging.getLogger(__name__)


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

        job_filter = (
            {"brain_patient": patient, "domain": "brain"}
            if domain == "brain"
            else {"patient": patient, "domain": "maxillo"}
        )

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

        updated = []
        not_found = []

        # Process each requested modality dynamically
        for modality_slug in requested_jobs:
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

                    # Update patient status fields if they exist (for backward compatibility)
                    status_field = f"{modality_slug}_processing_status"
                    if hasattr(patient, status_field):
                        setattr(patient, status_field, "processing")
                        patient.save()

                    # Special handling for dependencies
                    if hasattr(job, "update_status_based_on_dependencies"):
                        job.update_status_based_on_dependencies()

                    updated.append(modality_slug)
            except Exception as e:
                logger.error(f"Error processing job for modality {modality_slug}: {e}")

            # Handle special case for audio/voice captions (check via modality metadata)
            from ..modality_helpers import get_modality_by_slug

            modality_obj = get_modality_by_slug(modality_slug)
            is_audio_modality = False
            if modality_obj:
                metadata = getattr(modality_obj, "metadata", {}) or {}
                is_audio_modality = metadata.get(
                    "is_audio_modality", False
                ) or modality_slug in ["audio", "voice"]

            if is_audio_modality:
                # Use 'audio' as the canonical slug for job lookups
                actual_slug = "audio" if modality_slug == "voice" else modality_slug
                audio_jobs = Job.objects.filter(modality_slug=actual_slug, **job_filter)
                if audio_jobs.exists():
                    for job in audio_jobs:
                        job.status = "pending"
                        job.started_at = None
                        job.completed_at = None
                        job.worker_id = ""
                        job.error_logs = ""
                        job.save()
                    # Also reset related captions to pending
                    for vc in patient.voice_captions.all():
                        vc.processing_status = "pending"
                        vc.save()
                    if modality_slug not in updated:
                        updated.append(modality_slug)
                    jobs_found = True

            if not jobs_found and modality_slug not in updated:
                not_found.append(modality_slug)

        msg_parts = []
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
                "not_found": not_found,
            }
        )

    except Exception as e:
        logger.error(f"Error rerunning processing for scan {patient_id}: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


def admin_control_panel(request):
    """Admin control panel showing job stats."""
    from common.models import Job

    domain = get_namespace(request)

    # Get job statistics
    jobs = Job.objects.filter(domain=domain)
    job_stats = jobs.aggregate(
        total_jobs=Count("id"),
        pending_jobs=Count("id", filter=Q(status="pending")),
        processing_jobs=Count("id", filter=Q(status="processing")),
        completed_jobs=Count("id", filter=Q(status="completed")),
        failed_jobs=Count("id", filter=Q(status="failed")),
    )

    # Get job breakdown by type
    job_type_stats = (
        jobs.values("modality_slug")
        .annotate(
            total=Count("id"),
            pending=Count("id", filter=Q(status="pending")),
            processing=Count("id", filter=Q(status="processing")),
            completed=Count("id", filter=Q(status="completed")),
            failed=Count("id", filter=Q(status="failed")),
        )
        .order_by("modality_slug")
    )

    # Get recent failed jobs
    recent_failed_jobs = (
        jobs.filter(status="failed")
        .select_related(
            "patient", "brain_patient", "voice_caption", "brain_voice_caption"
        )
        .order_by("-created_at")[:10]
    )

    # Get processing queue info (dynamic by modality)
    from django.utils.text import slugify as _slugify
    from common.models import Modality as _Modality

    processing_queue = {}
    for _m in _Modality.objects.order_by("name"):
        _slug = _m.slug or _slugify(_m.name)
        processing_queue[_slug] = jobs.filter(
            modality_slug=_slug, status="pending"
        ).count()

    context = {
        "job_stats": job_stats,
        "job_type_stats": job_type_stats,
        "recent_failed_jobs": recent_failed_jobs,
        "processing_queue": processing_queue,
    }

    return render(request, "maxillo/admin_control_panel.html", context)
