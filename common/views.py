from django.shortcuts import render
from django.contrib.auth.models import User
from django.db.models import Count, Q
from django.db import connection
from django.utils.text import slugify
from django.utils import timezone

from .models import Job, ProcessingJob, Project, Modality
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
