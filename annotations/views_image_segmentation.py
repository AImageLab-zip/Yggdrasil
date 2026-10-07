"""HTTP for 2D labelmap segmentation: state, frame read, save, and the label vocabulary.

Thin, like ``annotations/views.py``: authorise, parse, hand off to
:mod:`annotations.services.image_segmentation`, translate its failures. The URL names a
patient and the work, never a viewer, and it serves every domain namespace.

Planes cross the wire as ``base64(gzip(width*height bytes of 0/1))`` inside JSON. The
browser has ``CompressionStream`` and no zip library, so the **server owns the archive
format**; the files in object storage are never handed out raw.
"""

import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST

from annotations.adapters import image_segmentation as codec
from annotations.constants import CoordinateSystem
from annotations.services import AnnotationConflict, AnnotationNotAllowed
from annotations.services import image_segmentation as service
from annotations.validators.image_segmentation import validate_time_ms
from annotations.views import (
    _BadRequest,
    _checked_file,
    _first_message,
    _patient_model,
)
from common.permissions import get_patient_for


def _json_body(request):
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        raise _BadRequest("Malformed JSON body")
    if not isinstance(body, dict):
        raise _BadRequest("Body must be a JSON object")
    return body


def _integer_or_none(body, name):
    value = body.get(name)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise _BadRequest(f"{name} must be an integer or null")
    return value


def _patient(request, patient_id, permission):
    return get_patient_for(request.user, _patient_model(request), patient_id, permission)


def _project(patient):
    project = getattr(patient, "project", None)
    if project is None:
        raise _BadRequest("This patient belongs to no project, so it has no labels.")
    return project


@login_required
def image_segmentation_state_api(request, patient_id):
    """The vocabulary, the revision a save must quote, and which frames hold masks."""
    patient = _patient(request, patient_id, "read")
    state = service.image_segmentation_state(patient)
    updated_at = state["updatedAt"]
    return JsonResponse({**state, "updatedAt": updated_at.isoformat() if updated_at else None})


@login_required
def image_segmentation_frame_api(request, patient_id):
    """One frame's planes, ``?fileId=<id>&timeMs=<ms>``. 404 when it has no mask."""
    patient = _patient(request, patient_id, "read")
    try:
        try:
            file_id = int(request.GET.get("fileId", ""))
            time_ms = int(request.GET.get("timeMs", ""))
        except ValueError:
            raise _BadRequest("fileId and timeMs must be integers")
        _checked_file(file_id, patient, "fileId")
        validate_time_ms(time_ms)
    except _BadRequest as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    except ValidationError as exc:
        return JsonResponse({"error": _first_message(exc)}, status=400)

    frame = service.read_frame(patient, file_id, time_ms)
    if frame is None:
        return JsonResponse({"error": "No mask is stored for that frame."}, status=404)
    return JsonResponse(
        {
            "fileId": file_id,
            "timeMs": time_ms,
            "width": frame["width"],
            "height": frame["height"],
            "planes": {code: codec.encode_plane(plane) for code, plane in frame["planes"].items()},
        }
    )


@login_required
@require_POST
def save_image_segmentation_api(request, patient_id):
    """Write the edited frames as a new revision.

    Body::

        {
          "fileId": 123,                    // the video (or image) the frames belong to
          "expectedRevision": 4,            // the revision the client loaded (0 for none)
          "coordinateSystem": "video_pixel",// or "image_pixel" for a still
          "fps": 25.0,                      // optional, recorded with the source
          "frames": [
            {"timeMs": 4200, "width": 1920, "height": 1080,
             "planes": {"l1": "<base64 gzip of width*height 0/1 bytes>"}}
          ]
        }

    **Only edited frames are named**; every other frame is carried forward. A frame whose
    planes are all empty is a deletion.
    """
    patient = _patient(request, patient_id, "write")
    try:
        body = _json_body(request)
        expected = _integer_or_none(body, "expectedRevision")
        file_obj = _checked_file(body.get("fileId"), patient, "fileId")
        raw_frames = body.get("frames")
        if not isinstance(raw_frames, list):
            raise _BadRequest("frames must be a list")
        frames = []
        for index, entry in enumerate(raw_frames):
            if not isinstance(entry, dict):
                raise _BadRequest(f"frames[{index}] must be an object")
            frames.append(
                {
                    "time_ms": entry.get("timeMs"),
                    "width": entry.get("width"),
                    "height": entry.get("height"),
                    "planes": entry.get("planes"),
                }
            )
        _project(patient)
    except _BadRequest as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)

    try:
        revision, manifest = service.save_image_segmentation(
            patient,
            source_file=file_obj,
            frames=frames,
            author=request.user,
            expected_revision=expected,
            coordinate_system=body.get("coordinateSystem", CoordinateSystem.VIDEO_PIXEL),
            fps=body.get("fps"),
        )
    except AnnotationConflict as exc:
        return JsonResponse({"error": str(exc), "conflict": True}, status=409)
    except AnnotationNotAllowed as exc:
        return JsonResponse({"error": str(exc)}, status=403)
    except ValidationError as exc:
        # Nothing was written: validation and decoding precede the first row.
        return JsonResponse({"error": _first_message(exc)}, status=400)

    return JsonResponse(
        {
            "revision": revision.revision_number,
            "setId": revision.annotation_set_id,
            "sources": manifest["sources"],
        }
    )


@login_required
@require_http_methods(["GET", "POST"])
def image_segmentation_labels_api(request, patient_id):
    """List (read) or create (write) the project's labels."""
    patient = _patient(request, patient_id, "read" if request.method == "GET" else "write")
    try:
        project = _project(patient)
        if request.method == "GET":
            return JsonResponse({"labels": service.list_labels(project)})
        body = _json_body(request)
        label = service.create_label(
            project, name=body.get("name"), color=body.get("color", "")
        )
    except _BadRequest as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    except ValidationError as exc:
        return JsonResponse({"error": _first_message(exc)}, status=400)
    return JsonResponse({"label": label}, status=201)


@login_required
@require_http_methods(["PATCH", "DELETE"])
def image_segmentation_label_detail_api(request, patient_id, code):
    """Rename/recolour/restore (PATCH) or retire (DELETE) one label.

    DELETE retires rather than deletes: the label's pixels stay in every revision that has
    them, and a deleted definition would leave planes that name nothing.
    """
    patient = _patient(request, patient_id, "write")
    try:
        project = _project(patient)
        if request.method == "DELETE":
            changes = {"active": False}
        else:
            body = _json_body(request)
            changes = {
                key: body[key] for key in ("name", "color", "active") if key in body
            }
        label = service.update_label(project, code, **changes)
    except _BadRequest as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    except LookupError:
        return JsonResponse({"error": "No such label."}, status=404)
    except ValidationError as exc:
        return JsonResponse({"error": _first_message(exc)}, status=400)
    return JsonResponse({"label": label})
