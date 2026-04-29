import json
import logging
from functools import wraps

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from common.models import Job
from maxillo.runner_api_service import (
    claim_job_for_runner,
    complete_job_from_runner,
    fail_job_from_runner,
)


logger = logging.getLogger(__name__)


def _extract_bearer_token(request):
    auth = (request.META.get("HTTP_AUTHORIZATION") or "").strip()
    if not auth:
        return ""
    parts = auth.split(" ", 1)
    if len(parts) != 2:
        return ""
    if parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def _runner_auth_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        allowed = getattr(settings, "RUNNER_API_TOKENS", set()) or set()
        token = _extract_bearer_token(request)
        if not allowed:
            return JsonResponse(
                {"error": "Runner API tokens are not configured"}, status=503
            )
        if not token or token not in allowed:
            return JsonResponse({"error": "Unauthorized"}, status=401)
        return view_func(request, *args, **kwargs)

    return _wrapped


def _worker_id_from_request(request):
    worker_id = (request.META.get("HTTP_X_RUNNER_WORKER_ID") or "").strip()
    if worker_id:
        return worker_id
    return "external-runner"


def _json_body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except Exception:
        return None


@csrf_exempt
@require_http_methods(["POST"])
@_runner_auth_required
def runner_claim_job(request, job_id: int):
    try:
        Job.objects.get(id=job_id)
    except Job.DoesNotExist:
        return JsonResponse({"error": "Job not found"}, status=404)

    worker_id = _worker_id_from_request(request)
    try:
        result = claim_job_for_runner(job_id=job_id, worker_id=worker_id)
    except Exception as e:
        logger.exception("Runner claim API failed for job %s", job_id)
        return JsonResponse({"error": f"Runner API internal error: {e}"}, status=500)
    status = 200 if result.get("claimed") else 409
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["POST"])
@_runner_auth_required
def runner_complete_job(request, job_id: int):
    try:
        Job.objects.get(id=job_id)
    except Job.DoesNotExist:
        return JsonResponse({"error": "Job not found"}, status=404)

    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    output_files = payload.get("output_files")
    if output_files is not None and not isinstance(output_files, dict):
        return JsonResponse({"error": "output_files must be an object"}, status=400)

    logs = payload.get("logs")
    if logs is not None and not isinstance(logs, str):
        return JsonResponse({"error": "logs must be a string"}, status=400)

    worker_id = _worker_id_from_request(request)
    try:
        result = complete_job_from_runner(
            job_id=job_id,
            worker_id=worker_id,
            output_files=output_files or {},
            logs=logs or "",
        )
    except Exception as e:
        logger.exception("Runner complete API failed for job %s", job_id)
        return JsonResponse({"error": f"Runner API internal error: {e}"}, status=500)
    status = 200 if result.get("completed") else 409
    return JsonResponse(result, status=status)


@csrf_exempt
@require_http_methods(["POST"])
@_runner_auth_required
def runner_fail_job(request, job_id: int):
    try:
        Job.objects.get(id=job_id)
    except Job.DoesNotExist:
        return JsonResponse({"error": "Job not found"}, status=404)

    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    error_msg = payload.get("error") or payload.get("error_msg") or "Runner error"
    if not isinstance(error_msg, str):
        return JsonResponse({"error": "error must be a string"}, status=400)

    worker_id = _worker_id_from_request(request)
    try:
        result = fail_job_from_runner(
            job_id=job_id, worker_id=worker_id, error_msg=error_msg
        )
    except Exception as e:
        logger.exception("Runner fail API failed for job %s", job_id)
        return JsonResponse({"error": f"Runner API internal error: {e}"}, status=500)
    status = 200 if result.get("failed") else 409
    return JsonResponse(result, status=status)
