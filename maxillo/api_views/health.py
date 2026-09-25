"""Health check API endpoint."""

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
import logging
from common.models import Job
from common.object_storage import get_object_storage

logger = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["GET"])
def health_check(request):
    """
    Health check endpoint
    URL: /api/processing/health/

    Anyone gets ``status`` (and 500 when unhealthy), which is all a monitor needs.
    Job counts and storage error text are operational detail -- raw storage
    errors name endpoints and buckets -- and are for staff only.
    """
    detail = bool(getattr(request.user, "is_staff", False))
    try:
        # Check database connectivity
        pending_count = Job.objects.filter(status="pending").count()
        processing_count = Job.objects.filter(status="processing").count()

        object_storage_ok = False
        object_storage_error = None
        try:
            storage = get_object_storage()
            storage._client.list_objects_v2(Bucket=storage.bucket, MaxKeys=1)
            object_storage_ok = True
        except Exception as e:
            object_storage_error = str(e)
            logger.warning("Health check: object storage unavailable: %s", e)

        body = {"success": True, "status": "healthy" if object_storage_ok else "degraded"}
        if detail:
            body.update(
                pending_jobs=pending_count,
                processing_jobs=processing_count,
                object_storage_ok=object_storage_ok,
                object_storage_error=object_storage_error,
            )
        return JsonResponse(body)

    except Exception as e:
        logger.exception("Health check failed: %s", e)
        body = {"success": False, "status": "unhealthy"}
        if detail:
            body["error"] = str(e)
        return JsonResponse(body, status=500)
