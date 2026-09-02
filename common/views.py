from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
from django.contrib.auth.models import User
from django.db.models import Count, Q
from django.db import connection
from django.utils.text import slugify
from django.utils import timezone

from datetime import timedelta

from . import presence
from .domains import project_admin_add_targets
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


def maintenance_page(request):
    """Public page used while the site is in full lockdown."""
    return render(
        request,
        "common/maintenance.html",
        {"hide_maintenance_banner": True},
        status=503,
    )


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


@login_required
@user_passes_test(lambda u: u.is_staff)
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

    # MySQL backup status (reuses the status-page helpers).
    from .models import SystemCheck

    # Project overview (admin tooling for the Project-first layout).
    from django.db.models import Count as _Count

    projects = (
        Project.objects.filter(is_active=True)
        .annotate(
            member_count=_Count("access_list"),
            modality_count=_Count("modalities"),
            annotation_count=_Count("annotation_methods"),
        )
        .order_by("domain", "name")
    )
    project_rows = []
    for project in projects:
        from .domains import patient_count_for as _patient_count_for

        project_rows.append(
            {
                "id": project.id,
                "name": project.name,
                "domain": project.domain,
                "slug": project.slug,
                "member_count": project.member_count,
                "modality_count": project.modality_count,
                "annotation_count": project.annotation_count,
                "patient_count": _patient_count_for(project) or 0,
            }
        )

    context = {
        "system_health": system_health,
        "job_counts": job_counts,
        "pending_by_modality": pending_by_modality,
        "user_count": user_count,
        "project_user_list": project_user_list,
        "projects": project_rows,
        "project_add_targets": project_admin_add_targets(),
        "backup": _backup_health(),
        "backup_inventory": _backup_inventory(),
        "recent_backups": SystemCheck.objects.filter(name="database_backup")[:8],
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
        "timeline": timeline,
    }
    return render(request, "common/user_activity_stats.html", context)


BACKUP_FRESHNESS_LIMIT_HOURS = 26


def _backup_health():
    """Latest backup run + freshness assessment for the status page."""
    from .models import SystemCheck

    latest = SystemCheck.objects.filter(name="database_backup").first()
    latest_ok = (
        SystemCheck.objects.filter(name="database_backup", status="ok").first()
    )
    if latest_ok is None:
        return {
            "status": "warn",
            "message": "No successful backup recorded yet",
            "latest": latest,
        }
    age = timezone.now() - latest_ok.ran_at
    if age > timedelta(hours=BACKUP_FRESHNESS_LIMIT_HOURS):
        hours = int(age.total_seconds() // 3600)
        return {
            "status": "warn",
            "message": f"Last successful backup is {hours}h old "
            f"(limit {BACKUP_FRESHNESS_LIMIT_HOURS}h)",
            "latest": latest,
        }
    return {
        "status": "ok" if latest and latest.status == "ok" else "warn",
        "message": f"Last successful backup at {latest_ok.ran_at:%Y-%m-%d %H:%M} UTC",
        "latest": latest,
    }


def _backup_inventory():
    """Count / size / newest of the MySQL backups retained in object storage.

    Mirrors the object-storage health check pattern; enumerates keys under
    settings.BACKUP_KEY_PREFIX so the admin panel can show how many backups are
    kept and when the most recent landed.
    """
    prefix = getattr(settings, "BACKUP_KEY_PREFIX", "backups/mysql/")
    inventory = {
        "available": False,
        "count": 0,
        "total_bytes": 0,
        "latest": None,
        "prefix": prefix,
        "keep_daily": getattr(settings, "BACKUP_KEEP_DAILY", None),
        "keep_weekly": getattr(settings, "BACKUP_KEEP_WEEKLY", None),
    }
    try:
        storage = get_object_storage()
        paginator = storage._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=storage.bucket, Prefix=storage.normalize_key(prefix)
        ):
            for obj in page.get("Contents", []) or []:
                inventory["count"] += 1
                inventory["total_bytes"] += obj.get("Size", 0) or 0
                modified = obj.get("LastModified")
                latest = inventory["latest"]
                if latest is None or (modified and modified > latest["modified"]):
                    inventory["latest"] = {
                        "key": obj.get("Key"),
                        "modified": modified,
                        "size": obj.get("Size", 0) or 0,
                    }
        inventory["available"] = True
    except Exception as exc:  # noqa: BLE001
        inventory["error"] = str(exc)
    return inventory


@login_required
@user_passes_test(lambda u: u.is_staff)
def status_page(request):
    """Staff-only system status dashboard."""
    from .models import SystemCheck

    return render(request, "common/status.html", {
        "database": _database_health(),
        "object_storage": _object_storage_health(),
        "backup": _backup_health(),
        "recent_checks": SystemCheck.objects.all()[:20],
        "checked_at": timezone.now(),
    })


def healthz(request):
    """Unauthenticated liveness/readiness probe: 200 or 503, no details."""
    healthy = (
        _database_health()["status"] == "up"
        and _object_storage_health()["status"] == "up"
    )
    return JsonResponse(
        {"status": "ok" if healthy else "unavailable"},
        status=200 if healthy else 503,
    )


@login_required
def set_report_language(request):
    """Cross-app AJAX endpoint: persist the user's Report Template language.

    Supersedes the old brain-only endpoint. Stores on the shared
    ``common.UserPreference`` so all three domains share one preference.
    """
    import json
    from .models import UserPreference

    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    try:
        body = json.loads(request.body)
        language = (body.get("language") or "it").strip()
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({"error": "Invalid request"}, status=400)

    if language not in ("it", "en", "de"):
        return JsonResponse({"error": "Invalid language"}, status=400)

    pref, _ = UserPreference.objects.get_or_create(user=request.user)
    pref.report_language = language
    pref.save(update_fields=["report_language", "updated_at"])
    return JsonResponse({"ok": True, "language": language})


@login_required
def notifications_api(request):
    """Return the current user's unread count + latest notifications (bell)."""
    from .models import Notification

    qs = Notification.objects.filter(user=request.user)
    unread = qs.filter(is_read=False).count()
    items = [
        {
            "id": n.id,
            "level": n.level,
            "message": n.message,
            "url": n.url,
            "is_read": n.is_read,
            "created_at": n.created_at.strftime("%b %d, %H:%M"),
        }
        for n in qs[:20]
    ]
    return JsonResponse({"unread": unread, "items": items})


@login_required
def notifications_mark_read(request):
    """Mark one (``id`` in body) or all of the user's notifications read."""
    import json
    from .models import Notification

    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    try:
        body = json.loads(request.body or "{}")
    except (json.JSONDecodeError, AttributeError):
        body = {}

    qs = Notification.objects.filter(user=request.user, is_read=False)
    nid = body.get("id")
    if nid:
        qs = qs.filter(id=nid)
    updated = qs.update(is_read=True)
    return JsonResponse({"ok": True, "marked": updated})
