from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
from django.contrib.auth.models import User
from django.db.models import Count, Q
from django.db import connection
from django.utils.text import slugify
from django.utils import timezone

import json
from datetime import timedelta

from . import presence
from .models import Job, ProcessingJob, Project, Modality, UserSession
from .object_storage import get_object_storage


def _database_health():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return {"status": "up", "message": "Connected"}
    except Exception as exc:
        return {"status": "down", "message": str(exc)}


def _object_storage_health():
    try:
        storage = get_object_storage()
        storage._client.list_objects_v2(Bucket=storage.bucket, MaxKeys=1)
        return {
            "status": "up",
            "label": "Up",
            "message": f"Bucket '{storage.bucket}' reachable",
        }
    except Exception as exc:
        return {
            "status": "down",
            "label": "Down",
            "message": str(exc),
        }


def admin_control_panel(request):
    """App-agnostic admin control panel with aggregated metrics."""
    system_health = {
        "object_storage": _object_storage_health(),
        "database": _database_health(),
        "checked_at": timezone.now(),
    }

    # Job counts (aggregate across Job and ProcessingJob)
    job_counts = {
        "pending": 0,
        "processing": 0,
        "completed": 0,
        "failed": 0,
    }

    # Aggregate from Job
    job_agg = Job.objects.aggregate(
        pending=Count("id", filter=Q(status="pending")),
        processing=Count("id", filter=Q(status="processing")),
        completed=Count("id", filter=Q(status="completed")),
        failed=Count("id", filter=Q(status="failed")),
    )
    for k in job_counts.keys():
        job_counts[k] += job_agg.get(k, 0) or 0

    # Aggregate from ProcessingJob
    proc_agg = ProcessingJob.objects.aggregate(
        pending=Count("id", filter=Q(status="pending")),
        processing=Count("id", filter=Q(status="processing")),
        completed=Count("id", filter=Q(status="completed")),
        failed=Count("id", filter=Q(status="failed")),
    )
    for k in job_counts.keys():
        job_counts[k] += proc_agg.get(k, 0) or 0

    job_counts["total"] = sum(job_counts.values())

    # Users
    user_count = User.objects.count()

    # Pending jobs per modality (iterate all modalities)
    pending_by_modality = []
    for modality in Modality.objects.order_by("name"):
        slug = modality.slug or slugify(modality.name)
        pending_jobs = (
            Job.objects.filter(modality_slug=slug, status="pending").count()
            + ProcessingJob.objects.filter(job_type=slug, status="pending").count()
        )
        pending_by_modality.append(
            {
                "slug": slug,
                "name": modality.name,
                "pending": pending_jobs,
            }
        )

    # Users per project (aggregated)
    projects_with_counts = Project.objects.annotate(
        num_users=Count("access_list__user", distinct=True)
    ).order_by("name")

    project_user_list = []
    for project in projects_with_counts:
        usernames = list(
            User.objects.filter(project_access__project=project)
            .values_list("username", flat=True)
            .order_by("username")
        )
        project_user_list.append(
            {
                "project_id": project.id,
                "project_name": project.name,
                "num_users": project.num_users,
                "usernames": usernames,
            }
        )

    context = {
        "system_health": system_health,
        "job_counts": job_counts,
        "pending_by_modality": pending_by_modality,
        "user_count": user_count,
        "project_user_list": project_user_list,
    }
    return render(request, "common/admin_control_panel.html", context)


@login_required
@user_passes_test(lambda u: u.is_staff)
def online_users_dashboard(request):
    """Admin-only live view of currently connected users."""
    return render(request, "common/online_users_dashboard.html", {
        "online_users": presence.get_online_users(),
    })


@login_required
@user_passes_test(lambda u: u.is_staff)
def online_users_api(request):
    """JSON feed polled by the live dashboard."""
    return JsonResponse({"users": presence.get_online_users()})


_PROJECT_LABELS = {"maxillo": "Maxillo", "brain": "Brain", "laparoscopy": "Laparoscopy", "": "Other"}


@login_required
@user_passes_test(lambda u: u.is_staff)
def user_activity_stats(request):
    """Admin-only per-user, per-project connected-time stats and timeline."""
    try:
        days = max(1, min(int(request.GET.get("days", 7)), 90))
    except ValueError:
        days = 7
    cutoff = timezone.now() - timedelta(days=days)

    selected_project = request.GET.get("project", "")  # "" means "all projects"
    sessions = UserSession.objects.filter(last_seen_at__gte=cutoff).select_related("user")
    if selected_project:
        sessions = sessions.filter(project_slug="" if selected_project == "other" else selected_project)

    totals = {}
    for session in sessions:
        key = (session.user_id, session.project_slug)
        entry = totals.setdefault(key, {
            "user_id": session.user_id,
            "username": session.user.username,
            "full_name": session.user.get_full_name() or session.user.username,
            "project_slug": session.project_slug,
            "project_label": _PROJECT_LABELS.get(session.project_slug, session.project_slug),
            "total_seconds": 0,
            "session_count": 0,
        })
        entry["total_seconds"] += session.duration_seconds
        entry["session_count"] += 1

    summary = sorted(totals.values(), key=lambda e: e["total_seconds"], reverse=True)

    view = request.GET.get("view") if request.GET.get("view") in {"single", "all"} else "single"
    selected_user_id = request.GET.get("user")
    timeline = []

    if view == "all":
        # Cap to the busiest users in this window so the shared timeline stays readable.
        top_user_ids = list({e["user_id"] for e in summary[:25]})
        all_sessions = sessions.filter(user_id__in=top_user_ids).order_by("started_at")
        timeline = [
            {
                "user_id": s.user_id,
                "username": s.user.username,
                "started_at": s.started_at.isoformat(),
                "last_seen_at": s.last_seen_at.isoformat(),
                "duration_seconds": s.duration_seconds,
                "project_slug": s.project_slug,
                "project_label": _PROJECT_LABELS.get(s.project_slug, s.project_slug),
            }
            for s in all_sessions
        ]
    elif selected_user_id:
        # Single-user mode: always show every project for that user — the
        # chart color-codes by project so mixing them is the point, not a bug.
        user_sessions = UserSession.objects.filter(
            user_id=selected_user_id,
            last_seen_at__gte=cutoff,
        ).order_by("started_at")
        timeline = [
            {
                "started_at": s.started_at.isoformat(),
                "last_seen_at": s.last_seen_at.isoformat(),
                "duration_seconds": s.duration_seconds,
                "project_slug": s.project_slug,
                "project_label": _PROJECT_LABELS.get(s.project_slug, s.project_slug),
            }
            for s in user_sessions
        ]

    context = {
        "days": days,
        "summary": summary,
        "selected_project": selected_project,
        "project_choices": [(slug, label) for slug, label in _PROJECT_LABELS.items() if slug],
        "view": view,
        "selected_user_id": int(selected_user_id) if selected_user_id else None,
        "timeline_json": json.dumps(timeline),
    }
    return render(request, "common/user_activity_stats.html", context)
