"""HTTP for the video quadrant timeline: state, save, and the quadrant vocabulary.

Thin, like ``views_image_segmentation.py``, whose helpers it shares. ``PUT`` replaces the
whole marker list, exactly as the timeline sends it.
"""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from annotations.services import AnnotationConflict, AnnotationNotAllowed
from annotations.services import quadrants as service
from annotations.views import _BadRequest, _checked_file, _first_message
from annotations.views_image_segmentation import (
    _integer_or_none,
    _json_body,
    _patient,
    _project,
)


@login_required
@require_http_methods(["GET", "PUT"])
def quadrants_api(request, patient_id):
    """GET: the vocabulary, the revision to quote and the markers. PUT: replace the markers.

    PUT body: ``{"fileId": 123, "expectedRevision": 4, "markers": [{"timeMs": 0, "code": "l1"}]}``
    """
    patient = _patient(request, patient_id, "read" if request.method == "GET" else "write")
    if request.method == "GET":
        return JsonResponse(service.quadrant_state(patient))
    try:
        body = _json_body(request)
        expected = _integer_or_none(body, "expectedRevision")
        file_obj = _checked_file(body.get("fileId"), patient, "fileId")
        _project(patient)
    except _BadRequest as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    try:
        service.save_quadrant_markers(
            patient,
            source_file=file_obj,
            markers=body.get("markers"),
            author=request.user,
            expected_revision=expected,
        )
    except AnnotationConflict as exc:
        return JsonResponse({"error": str(exc), "conflict": True}, status=409)
    except AnnotationNotAllowed as exc:
        return JsonResponse({"error": str(exc)}, status=403)
    except ValidationError as exc:
        return JsonResponse({"error": _first_message(exc)}, status=400)
    return JsonResponse(service.quadrant_state(patient))


@login_required
@require_http_methods(["POST"])
def quadrant_labels_api(request, patient_id):
    """Create a quadrant (write). The list comes with the state."""
    patient = _patient(request, patient_id, "write")
    try:
        body = _json_body(request)
        label = service.create_label(
            _project(patient), name=body.get("name"), color=body.get("color", "")
        )
    except _BadRequest as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    except ValidationError as exc:
        return JsonResponse({"error": _first_message(exc)}, status=400)
    return JsonResponse({"label": label}, status=201)


@login_required
@require_http_methods(["PATCH", "DELETE"])
def quadrant_label_detail_api(request, patient_id, code):
    """Rename/recolour/restore (PATCH) or retire (DELETE) one quadrant."""
    patient = _patient(request, patient_id, "write")
    try:
        project = _project(patient)
        if request.method == "DELETE":
            changes = {"active": False}
        else:
            body = _json_body(request)
            changes = {k: body[k] for k in ("name", "color", "active") if k in body}
        label = service.update_label(project, code, **changes)
    except _BadRequest as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    except LookupError:
        return JsonResponse({"error": "No such quadrant."}, status=404)
    except ValidationError as exc:
        return JsonResponse({"error": _first_message(exc)}, status=400)
    return JsonResponse({"label": label})
