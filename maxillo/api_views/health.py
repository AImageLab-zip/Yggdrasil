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
    Simple health check endpoint
    URL: /api/processing/health/
    """
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

        return JsonResponse(
            {
                "success": True,
                "status": "healthy",
                "pending_jobs": pending_count,
                "processing_jobs": processing_count,
                "object_storage_ok": object_storage_ok,
                "object_storage_error": object_storage_error,
            }
        )

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        import traceback

        logger.error(f"Full traceback: {traceback.format_exc()}")
        return JsonResponse(
            {"success": False, "status": "unhealthy", "error": str(e)}, status=500
        )
