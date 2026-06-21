import json
import logging
import math
import os
import re
import base64
from urllib import error as urllib_error
from urllib import request as urllib_request

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from laparoscopy.models import (
    QuadrantClassificationMarker,
    QuadrantType,
    QuadrantTypeUserColor,
    RegionAnnotation,
    RegionType,
    RegionTypeUserColor,
)


logger = logging.getLogger(__name__)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _get_profile(request):
    return getattr(request.user, "profile", None)


def _parse_json_body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Invalid JSON body")


def _worker_url(request, path, specific_env):
    specific = (os.getenv(specific_env) or "").strip()
    if specific:
        return specific
    base = (os.getenv("WORKER_BASE_URL") or "").strip()
    if not base:
        host = (request.get_host() or "localhost").split(":", 1)[0]
        scheme = "https" if request.is_secure() else "http"
        base = f"{scheme}://{host}"
    return f"{base.rstrip('/')}{path}"


def _worker_json_request(worker_url, payload, timeout):
    req = urllib_request.Request(
        worker_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        status_code = resp.getcode()
        body = resp.read().decode("utf-8")
    try:
        worker_response = json.loads(body) if body else {}
    except json.JSONDecodeError:
        worker_response = body
    return status_code, worker_response


def _is_hex_color(value):
    return isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) is not None


def _next_type_order(model_cls, project):
    last_order = (
        model_cls.objects.filter(project=project)
        .order_by("-order")
        .values_list("order", flat=True)
        .first()
    )
    return 0 if last_order is None else last_order + 1


def _patient_model():
    from django.apps import apps
    return apps.get_model("laparoscopy", "Patient")


def _patient_permissions(profile, patient):
    if not profile:
        return False, False
    can_view = False
    if profile.is_admin():
        can_view = True
    elif profile.is_annotator() and patient.visibility != "debug":
        can_view = True
    elif profile.is_student_developer() and patient.visibility == "debug":
        can_view = True
    elif patient.visibility == "public":
        can_view = True
    return can_view, can_view


def _normalize_float(value, field_name):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be numeric")
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be finite")
    return parsed


# ─── types helpers ────────────────────────────────────────────────────────────

def _types_payload(project, user, TypeModel, ColorModel, type_fk):
    types = list(TypeModel.objects.filter(project=project).order_by("order", "name"))
    user_colors = {
        getattr(pref, type_fk + "_id"): pref.color
        for pref in ColorModel.objects.filter(**{type_fk + "__project": project}, user=user)
    }
    return [{"id": t.id, "name": t.name, "color": user_colors.get(t.id, t.color)} for t in types]


def _handle_type_list(request, TypeModel, ColorModel, type_fk, default_color):
    profile = _get_profile(request)
    if not profile:
        return JsonResponse({"error": "No active project"}, status=403)

    if request.method == "GET":
        return JsonResponse(
            {"types": _types_payload(profile.project, request.user, TypeModel, ColorModel, type_fk)}
        )

    if not profile.is_admin():
        return JsonResponse({"error": "Administrator access required"}, status=403)

    try:
        data = _parse_json_body(request)
    except ValueError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    name = (data.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "name is required"}, status=400)

    color = data.get("color", default_color)
    if not _is_hex_color(color):
        return JsonResponse({"error": "color must be a hex value like " + default_color}, status=400)

    obj, created = TypeModel.objects.get_or_create(
        project=profile.project,
        name=name,
        defaults={"color": color, "order": _next_type_order(TypeModel, profile.project)},
    )
    if not created and obj.color != color:
        obj.color = color
        obj.save(update_fields=["color"])

    return JsonResponse({"id": obj.id, "name": obj.name, "color": obj.color}, status=201 if created else 200)


def _handle_type_detail(request, pk, TypeModel, ColorModel, type_fk, conflict_msg, before_delete=None):
    profile = _get_profile(request)
    if not profile:
        return JsonResponse({"error": "No active project"}, status=403)

    obj = get_object_or_404(TypeModel, pk=pk, project=profile.project)

    if request.method == "PATCH":
        try:
            data = _parse_json_body(request)
        except ValueError:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)

        has_name = "name" in data
        has_color = "color" in data
        if not has_name and not has_color:
            return JsonResponse({"error": "At least one of name or color is required"}, status=400)

        if has_name and not profile.is_admin():
            return JsonResponse({"error": "Administrator access required for rename"}, status=403)

        if has_name:
            name = (data.get("name") or "").strip()
            if not name:
                return JsonResponse({"error": "name cannot be empty"}, status=400)
            obj.name = name
            try:
                obj.save()
            except IntegrityError:
                return JsonResponse({"error": conflict_msg}, status=400)

        if has_color:
            color = data.get("color")
            if not _is_hex_color(color):
                return JsonResponse({"error": "color must be a hex value like #3498db"}, status=400)
            ColorModel.objects.update_or_create(
                **{type_fk: obj, "user": request.user}, defaults={"color": color}
            )

        effective_color = (
            ColorModel.objects.filter(**{type_fk: obj}, user=request.user)
            .values_list("color", flat=True)
            .first()
            or obj.color
        )
        return JsonResponse({"id": obj.id, "name": obj.name, "color": effective_color})

    # DELETE
    if not profile.is_admin():
        return JsonResponse({"error": "Administrator access required"}, status=403)

    if before_delete is not None:
        maybe_response = before_delete(request, profile, obj)
        if maybe_response is not None:
            return maybe_response

    obj.delete()
    return HttpResponse(status=204)


def _quadrant_type_delete_hook(request, profile, obj):
    markers_qs = QuadrantClassificationMarker.objects.filter(quadrant_type=obj)
    if not markers_qs.exists():
        return None

    try:
        data = _parse_json_body(request)
    except ValueError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    replacement_id = data.get("replacement_id")
    if replacement_id in [None, ""]:
        return JsonResponse({"error": "replacement_id is required to delete a type in use"}, status=400)
    try:
        replacement_id = int(replacement_id)
    except (TypeError, ValueError):
        return JsonResponse({"error": "replacement_id must be an integer"}, status=400)

    if replacement_id == obj.id:
        return JsonResponse({"error": "replacement_id must differ from the deleted type"}, status=400)

    replacement = QuadrantType.objects.filter(id=replacement_id, project=profile.project).first()
    if replacement is None:
        return JsonResponse({"error": "replacement_id must belong to the active project"}, status=400)

    markers_qs.update(quadrant_type=replacement, updated_by=request.user)
    return None


# ─── types views ──────────────────────────────────────────────────────────────

@login_required
@require_http_methods(["GET", "POST"])
def region_types(request):
    return _handle_type_list(request, RegionType, RegionTypeUserColor, "region_type", "#3498db")


@login_required
@require_http_methods(["PATCH", "DELETE"])
def region_type_detail(request, pk):
    return _handle_type_detail(
        request, pk, RegionType, RegionTypeUserColor, "region_type",
        "A region type with this name already exists",
    )


@login_required
@require_http_methods(["GET", "POST"])
def quadrant_types(request):
    return _handle_type_list(request, QuadrantType, QuadrantTypeUserColor, "quadrant_type", "#e74c3c")


@login_required
@require_http_methods(["PATCH", "DELETE"])
def quadrant_type_detail(request, pk):
    return _handle_type_detail(
        request, pk, QuadrantType, QuadrantTypeUserColor, "quadrant_type",
        "A quadrant type with this name already exists",
        before_delete=_quadrant_type_delete_hook,
    )


# ─── annotation helpers ───────────────────────────────────────────────────────

def _normalize_points(points):
    if not isinstance(points, list):
        raise ValueError("points must be a list")
    if len(points) < 4:
        raise ValueError("points must include at least two vertices")
    if len(points) % 2 != 0:
        raise ValueError("points length must be even")
    normalized = []
    for value in points:
        if isinstance(value, bool):
            raise ValueError("points must contain numeric values")
        normalized.append(_normalize_float(value, "points"))
    return normalized


def _normalize_prompt_points(prompt_points):
    if prompt_points is None:
        return []
    if not isinstance(prompt_points, list):
        raise ValueError("prompt_points must be a list")
    normalized = []
    for idx, raw_point in enumerate(prompt_points):
        prefix = f"prompt_points[{idx}]"
        if not isinstance(raw_point, dict):
            raise ValueError(f"{prefix} must be an object")
        if "x" not in raw_point or "y" not in raw_point:
            raise ValueError(f"{prefix} must include x and y")
        x = _normalize_float(raw_point.get("x"), f"{prefix}.x")
        y = _normalize_float(raw_point.get("y"), f"{prefix}.y")
        if x < 0 or x > 1 or y < 0 or y > 1:
            raise ValueError(f"{prefix} coordinates must be normalized between 0 and 1")
        raw_label = raw_point.get("label", 1)
        if isinstance(raw_label, bool):
            raise ValueError(f"{prefix}.label must be 0 or 1")
        try:
            label = int(raw_label)
        except (TypeError, ValueError):
            raise ValueError(f"{prefix}.label must be an integer")
        if label not in (0, 1):
            raise ValueError(f"{prefix}.label must be 0 or 1")
        normalized.append({"x": x, "y": y, "label": label})
    return normalized


def _annotation_payload(annotation):
    return {
        "id": annotation.id,
        "patient_id": annotation.patient_id,
        "region_type_id": annotation.region_type_id,
        "region_type_name": annotation.region_type.name,
        "tool": annotation.tool,
        "frame_time": annotation.frame_time,
        "points": annotation.points,
        "prompt_points": annotation.prompt_points,
        "stroke_width": annotation.stroke_width,
        "created_by_id": annotation.created_by_id,
        "created_by_username": annotation.created_by.username if annotation.created_by_id else None,
        "updated_by_id": annotation.updated_by_id,
        "updated_by_username": annotation.updated_by.username if annotation.updated_by_id else None,
        "created_at": annotation.created_at.isoformat() if annotation.created_at else None,
        "updated_at": annotation.updated_at.isoformat() if annotation.updated_at else None,
    }


def _quadrant_marker_payload(marker):
    return {
        "id": marker.id,
        "patient_id": marker.patient_id,
        "quadrant_type_id": marker.quadrant_type_id,
        "time_ms": marker.time_ms,
    }


def _normalize_time_ms(value):
    if isinstance(value, bool):
        raise ValueError("time_ms must be numeric")
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError("time_ms must be numeric")
    if parsed < 0:
        raise ValueError("time_ms must be >= 0")
    return parsed


def _normalize_quadrant_marker_items(raw_markers, project):
    if not isinstance(raw_markers, list):
        raise ValueError("markers must be a list")

    parsed_items = []
    quadrant_type_ids = set()

    for index, raw in enumerate(raw_markers):
        if not isinstance(raw, dict):
            raise ValueError("each marker must be an object")

        raw_marker_id = raw.get("id")
        marker_id = None
        if raw_marker_id not in [None, ""]:
            try:
                marker_id = int(raw_marker_id)
            except (TypeError, ValueError):
                raise ValueError("marker id must be an integer")
            if marker_id <= 0:
                raise ValueError("marker id must be > 0")

        raw_quadrant_type_id = raw.get("quadrant_type_id")
        if raw_quadrant_type_id in [None, ""]:
            raise ValueError("quadrant_type_id is required")
        try:
            quadrant_type_id = int(raw_quadrant_type_id)
        except (TypeError, ValueError):
            raise ValueError("quadrant_type_id must be an integer")

        time_ms = _normalize_time_ms(raw.get("time_ms"))
        parsed = {"order": index, "id": marker_id, "quadrant_type_id": quadrant_type_id, "time_ms": time_ms}
        parsed_items.append(parsed)
        quadrant_type_ids.add(quadrant_type_id)

    quadrant_types = {
        obj.id: obj
        for obj in QuadrantType.objects.filter(project=project, id__in=quadrant_type_ids)
    }
    if len(quadrant_types) != len(quadrant_type_ids):
        raise ValueError("quadrant_type_id must belong to the active project")

    for item in parsed_items:
        item["quadrant_type"] = quadrant_types[item["quadrant_type_id"]]

    sorted_items = sorted(parsed_items, key=lambda item: (item["time_ms"], item["order"]))

    dedup_same_time = []
    for item in sorted_items:
        if dedup_same_time and dedup_same_time[-1]["time_ms"] == item["time_ms"]:
            dedup_same_time[-1] = item
        else:
            dedup_same_time.append(item)

    compacted = []
    for item in dedup_same_time:
        if compacted and compacted[-1]["quadrant_type_id"] == item["quadrant_type_id"]:
            continue
        compacted.append(item)

    return compacted


def _replace_patient_quadrant_markers(patient, user, marker_items):
    with transaction.atomic():
        keep_ids = []
        for item in marker_items:
            marker, created = QuadrantClassificationMarker.objects.update_or_create(
                patient=patient,
                time_ms=item["time_ms"],
                defaults={"quadrant_type": item["quadrant_type"], "updated_by": user},
            )
            if created:
                marker.created_by = user
                marker.save(update_fields=["created_by"])
            keep_ids.append(marker.id)

        stale_qs = QuadrantClassificationMarker.objects.filter(patient=patient)
        if keep_ids:
            stale_qs.exclude(id__in=keep_ids).delete()
        else:
            stale_qs.delete()

    return list(
        QuadrantClassificationMarker.objects.filter(patient=patient)
        .select_related("quadrant_type")
        .order_by("time_ms", "id")
    )


# ─── annotation views ─────────────────────────────────────────────────────────

@login_required
@require_http_methods(["GET", "PUT"])
def patient_quadrant_markers(request, patient_id):
    profile = _get_profile(request)
    if not profile:
        return JsonResponse({"error": "No active project"}, status=403)

    Patient = _patient_model()
    patient = get_object_or_404(Patient, patient_id=patient_id)
    can_view, can_modify = _patient_permissions(profile, patient)
    if not can_view:
        return JsonResponse({"error": "Permission denied"}, status=403)

    if request.method == "GET":
        markers = (
            QuadrantClassificationMarker.objects.filter(patient=patient)
            .select_related("quadrant_type")
            .order_by("time_ms", "id")
        )
        return JsonResponse({"markers": [_quadrant_marker_payload(m) for m in markers]})

    if not can_modify:
        return JsonResponse({"error": "Permission denied"}, status=403)

    try:
        data = _parse_json_body(request)
    except ValueError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    try:
        normalized_items = _normalize_quadrant_marker_items(data.get("markers"), profile.project)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    markers = _replace_patient_quadrant_markers(patient=patient, user=request.user, marker_items=normalized_items)
    return JsonResponse({"markers": [_quadrant_marker_payload(m) for m in markers]})


@login_required
@require_http_methods(["GET", "POST"])
def patient_region_annotations(request, patient_id):
    profile = _get_profile(request)
    if not profile:
        return JsonResponse({"error": "No active project"}, status=403)

    Patient = _patient_model()
    patient = get_object_or_404(Patient, patient_id=patient_id)
    can_view, can_modify = _patient_permissions(profile, patient)
    if not can_view:
        return JsonResponse({"error": "Permission denied"}, status=403)

    if request.method == "GET":
        annotations = (
            RegionAnnotation.objects.filter(patient=patient)
            .select_related("region_type", "created_by", "updated_by")
            .order_by("created_at")
        )
        return JsonResponse({"annotations": [_annotation_payload(a) for a in annotations]})

    if not can_modify:
        return JsonResponse({"error": "Permission denied"}, status=403)

    try:
        data = _parse_json_body(request)
    except ValueError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    region_type_id = data.get("region_type_id")
    if region_type_id in [None, ""]:
        return JsonResponse({"error": "region_type_id is required"}, status=400)
    try:
        region_type_id = int(region_type_id)
    except (TypeError, ValueError):
        return JsonResponse({"error": "region_type_id must be an integer"}, status=400)

    tool = str(data.get("tool") or "").strip().lower()
    allowed_tools = {value for value, _ in RegionAnnotation.TOOL_CHOICES}
    if tool not in allowed_tools:
        return JsonResponse({"error": "tool must be one of brush, eraser, polygon"}, status=400)

    try:
        frame_time = _normalize_float(data.get("frame_time", 0.0), "frame_time")
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if frame_time < 0:
        return JsonResponse({"error": "frame_time must be >= 0"}, status=400)

    try:
        points = _normalize_points(data.get("points"))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    try:
        prompt_points = _normalize_prompt_points(data.get("prompt_points", []))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    if tool == "polygon" and len(points) < 6:
        return JsonResponse({"error": "polygon requires at least three vertices"}, status=400)

    try:
        stroke_width = _normalize_float(data.get("stroke_width", 1.0), "stroke_width")
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if stroke_width <= 0:
        return JsonResponse({"error": "stroke_width must be > 0"}, status=400)

    region_type = get_object_or_404(RegionType, id=region_type_id, project=profile.project)

    annotation = RegionAnnotation.objects.create(
        patient=patient, region_type=region_type, tool=tool,
        frame_time=frame_time, points=points, prompt_points=prompt_points,
        stroke_width=stroke_width, created_by=request.user, updated_by=request.user,
    )
    annotation = RegionAnnotation.objects.select_related(
        "region_type", "created_by", "updated_by"
    ).get(id=annotation.id)
    return JsonResponse(_annotation_payload(annotation), status=201)


@login_required
@require_http_methods(["PATCH", "DELETE"])
def region_annotation_detail(request, annotation_id):
    profile = _get_profile(request)
    if not profile:
        return JsonResponse({"error": "No active project"}, status=403)

    annotation = get_object_or_404(
        RegionAnnotation.objects.select_related("patient", "region_type", "created_by", "updated_by"),
        id=annotation_id,
    )
    patient = annotation.patient
    can_view, can_modify = _patient_permissions(profile, patient)
    if not can_view:
        return JsonResponse({"error": "Permission denied"}, status=403)

    if request.method == "DELETE":
        if not can_modify:
            return JsonResponse({"error": "Permission denied"}, status=403)
        annotation.delete()
        return HttpResponse(status=204)

    if not can_modify:
        return JsonResponse({"error": "Permission denied"}, status=403)

    try:
        data = _parse_json_body(request)
    except ValueError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    has_region = "region_type_id" in data
    has_points = "points" in data
    has_prompt_points = "prompt_points" in data
    has_frame_time = "frame_time" in data
    has_stroke_width = "stroke_width" in data

    if not (has_region or has_points or has_prompt_points or has_frame_time or has_stroke_width):
        return JsonResponse(
            {"error": "At least one of region_type_id, points, prompt_points, frame_time, stroke_width is required"},
            status=400,
        )

    changes = {}

    if has_region:
        try:
            region_type_id = int(data.get("region_type_id"))
        except (TypeError, ValueError):
            return JsonResponse({"error": "region_type_id must be an integer"}, status=400)
        region_type = get_object_or_404(RegionType, id=region_type_id, project=profile.project)
        if region_type.id != annotation.region_type_id:
            annotation.region_type = region_type
            changes["region_type_id"] = region_type.id

    if has_points:
        try:
            points = _normalize_points(data.get("points"))
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        if annotation.tool == "polygon" and len(points) < 6:
            return JsonResponse({"error": "polygon requires at least three vertices"}, status=400)
        annotation.points = points
        changes["points"] = points

    if has_prompt_points:
        try:
            prompt_points = _normalize_prompt_points(data.get("prompt_points"))
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        annotation.prompt_points = prompt_points
        changes["prompt_points"] = prompt_points

    if has_frame_time:
        try:
            frame_time = _normalize_float(data.get("frame_time"), "frame_time")
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        if frame_time < 0:
            return JsonResponse({"error": "frame_time must be >= 0"}, status=400)
        annotation.frame_time = frame_time
        changes["frame_time"] = frame_time

    if has_stroke_width:
        try:
            stroke_width = _normalize_float(data.get("stroke_width"), "stroke_width")
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        if stroke_width <= 0:
            return JsonResponse({"error": "stroke_width must be > 0"}, status=400)
        annotation.stroke_width = stroke_width
        changes["stroke_width"] = stroke_width

    if not changes:
        return JsonResponse(_annotation_payload(annotation))

    annotation.updated_by = request.user
    annotation.save()
    annotation.refresh_from_db()
    return JsonResponse(_annotation_payload(annotation))


# ─── Magic Tool worker proxy ──────────────────────────────────────────────────

@login_required
@csrf_exempt
@require_http_methods(["POST"])
def worker_session_ready(request):
    profile = _get_profile(request)
    if not profile:
        return JsonResponse({"error": "No active project"}, status=403)

    try:
        data = _parse_json_body(request)
    except ValueError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    patient_id = data.get("patientId")
    video_source = (data.get("video_source") or "").strip()
    video_id = (data.get("video_id") or "").strip()

    if patient_id is None:
        return JsonResponse({"error": "patientId is required"}, status=400)
    if not video_source:
        return JsonResponse({"error": "video_source is required"}, status=400)
    if not video_id:
        return JsonResponse({"error": "video_id is required"}, status=400)

    try:
        patient_id = int(patient_id)
    except (TypeError, ValueError):
        return JsonResponse({"error": "patientId must be an integer"}, status=400)

    Patient = _patient_model()
    patient = get_object_or_404(Patient, patient_id=patient_id)
    can_view, _ = _patient_permissions(profile, patient)
    if not can_view:
        return JsonResponse({"error": "Permission denied"}, status=403)

    worker_url = _worker_url(request, "/api/session/ready/", "WORKER_SESSION_READY_URL")
    try:
        status_code, worker_response = _worker_json_request(
            worker_url,
            {"video_source": video_source, "video_id": video_id},
            timeout=10,
        )
    except urllib_error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        logger.error(
            "Worker session-ready HTTP error %s for laparoscopy patient %s: %s",
            exc.code,
            patient.patient_id,
            error_body,
        )
        return JsonResponse(
            {
                "error": "Worker session ready request failed",
                "worker_status": exc.code,
                "worker_response": error_body,
            },
            status=502,
        )
    except urllib_error.URLError as exc:
        logger.error(
            "Worker session-ready network error for laparoscopy patient %s: %s",
            patient.patient_id,
            exc,
        )
        return JsonResponse(
            {"error": "Worker service unavailable", "details": str(exc)},
            status=502,
        )

    return JsonResponse(
        {"success": True, "worker_status": status_code, "worker_response": worker_response}
    )


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def worker_session_prompt(request):
    profile = _get_profile(request)
    if not profile:
        return JsonResponse({"error": "No active project"}, status=403)

    try:
        data = _parse_json_body(request)
    except ValueError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    patient_id = data.get("patientId")
    video_id = (data.get("video_id") or "").strip()
    frame_timestamp_raw = data.get("frame_timestamp")
    window_seconds_raw = data.get("window_seconds", 5.0)
    normalized_default = bool(data.get("normalized", True))

    if patient_id is None:
        return JsonResponse({"error": "patientId is required"}, status=400)
    if not video_id:
        return JsonResponse({"error": "video_id is required"}, status=400)

    try:
        patient_id = int(patient_id)
    except (TypeError, ValueError):
        return JsonResponse({"error": "patientId must be an integer"}, status=400)

    try:
        frame_timestamp = float(frame_timestamp_raw)
    except (TypeError, ValueError):
        return JsonResponse({"error": "frame_timestamp must be numeric"}, status=400)
    if frame_timestamp < 0:
        return JsonResponse({"error": "frame_timestamp must be >= 0"}, status=400)

    try:
        window_seconds = float(window_seconds_raw)
    except (TypeError, ValueError):
        return JsonResponse({"error": "window_seconds must be numeric"}, status=400)
    if window_seconds <= 0:
        return JsonResponse({"error": "window_seconds must be > 0"}, status=400)

    def _parse_points(points_raw, point_labels_raw, normalized_flag, prefix):
        if points_raw is None and point_labels_raw is None:
            return None, None, None
        if not isinstance(points_raw, list) or not points_raw:
            return None, None, f"{prefix}points must be a non-empty list"
        if not isinstance(point_labels_raw, list):
            return None, None, f"{prefix}point_labels must be a list"
        if len(point_labels_raw) != len(points_raw):
            return None, None, f"{prefix}point_labels length must match points length"

        points = []
        labels = []
        for idx, point in enumerate(points_raw):
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                return None, None, f"{prefix}points[{idx}] must be a [x, y] pair"
            try:
                x = float(point[0])
                y = float(point[1])
            except (TypeError, ValueError):
                return None, None, f"{prefix}points[{idx}] must contain numeric values"
            if normalized_flag and (x < 0 or x > 1 or y < 0 or y > 1):
                return None, None, f"{prefix}points[{idx}] must be normalized between 0 and 1"
            points.append([x, y])

        for idx, label in enumerate(point_labels_raw):
            if isinstance(label, bool):
                return None, None, f"{prefix}point_labels[{idx}] must be 0 or 1"
            try:
                parsed_label = int(label)
            except (TypeError, ValueError):
                return None, None, f"{prefix}point_labels[{idx}] must be an integer"
            if parsed_label not in (0, 1):
                return None, None, f"{prefix}point_labels[{idx}] must be 0 or 1"
            labels.append(parsed_label)

        return points, labels, None

    def _parse_box(box_raw, normalized_flag, prefix):
        if box_raw is None:
            return None, None
        if not isinstance(box_raw, list) or len(box_raw) != 2:
            return None, f"{prefix}box must be [[x1, y1], [x2, y2]]"

        parsed_box = []
        for corner_idx, corner in enumerate(box_raw):
            if not isinstance(corner, (list, tuple)) or len(corner) != 2:
                return None, f"{prefix}box[{corner_idx}] must be [x, y]"
            try:
                x = float(corner[0])
                y = float(corner[1])
            except (TypeError, ValueError):
                return None, f"{prefix}box[{corner_idx}] must contain numeric values"
            if normalized_flag and (x < 0 or x > 1 or y < 0 or y > 1):
                return None, f"{prefix}box[{corner_idx}] must be normalized between 0 and 1"
            parsed_box.append([x, y])
        return parsed_box, None

    def _parse_mask(mask_b64_raw, mask_shape_raw, mask_encoding_raw, prefix):
        if mask_b64_raw is None and mask_shape_raw is None and mask_encoding_raw is None:
            return None, None, None, None
        if not mask_b64_raw:
            return None, None, None, f"{prefix}mask_b64 is required when mask_shape is provided"
        if mask_encoding_raw is None:
            return None, None, None, f"{prefix}mask_encoding is required when mask_b64 is provided"
        if not isinstance(mask_shape_raw, list) or len(mask_shape_raw) != 2:
            return None, None, None, f"{prefix}mask_shape must be [height, width]"
        try:
            mask_h = int(mask_shape_raw[0])
            mask_w = int(mask_shape_raw[1])
        except (TypeError, ValueError):
            return None, None, None, f"{prefix}mask_shape values must be integers"
        if mask_h <= 0 or mask_w <= 0:
            return None, None, None, f"{prefix}mask_shape values must be > 0"

        mask_encoding = str(mask_encoding_raw)
        if mask_encoding != "bitpack_u1_v1":
            return None, None, None, f"{prefix}mask_encoding must be bitpack_u1_v1"

        try:
            mask_bytes = base64.b64decode(str(mask_b64_raw), validate=True)
        except Exception:
            return None, None, None, f"{prefix}mask_b64 must be valid base64"

        expected_bytes = (mask_h * mask_w + 7) // 8
        if len(mask_bytes) != expected_bytes:
            return (
                None,
                None,
                None,
                f"{prefix}mask_b64 byte length must match bit-packed mask_shape (expected {expected_bytes}, got {len(mask_bytes)})",
            )
        return str(mask_b64_raw), [mask_h, mask_w], mask_encoding, None

    regions_raw = data.get("regions")
    legacy_prompt_fields = (
        "region_id",
        "class_name",
        "class_id",
        "points",
        "point_labels",
        "box",
        "mask_b64",
        "mask_encoding",
        "mask_shape",
    )

    if regions_raw is not None and any(field in data for field in legacy_prompt_fields):
        return JsonResponse(
            {"error": "use either regions or top-level prompt fields, not both"},
            status=400,
        )

    if regions_raw is None:
        regions_raw = [
            {
                "region_id": data.get("region_id", "1"),
                "class_name": data.get("class_name", "unknown"),
                "class_id": data.get("class_id"),
                "points": data.get("points"),
                "point_labels": data.get("point_labels"),
                "box": data.get("box"),
                "mask_b64": data.get("mask_b64"),
                "mask_encoding": data.get("mask_encoding"),
                "mask_shape": data.get("mask_shape"),
                "normalized": normalized_default,
            }
        ]

    if not isinstance(regions_raw, list) or not regions_raw:
        return JsonResponse({"error": "regions must be a non-empty list"}, status=400)

    regions_payload = []
    seen_region_ids = set()
    for ridx, region_raw in enumerate(regions_raw):
        prefix = f"regions[{ridx}]."
        if not isinstance(region_raw, dict):
            return JsonResponse({"error": f"regions[{ridx}] must be an object"}, status=400)

        region_id = region_raw.get("region_id")
        if region_id is None or str(region_id).strip() == "":
            return JsonResponse({"error": f"{prefix}region_id is required"}, status=400)
        region_id = str(region_id).strip()
        if region_id in seen_region_ids:
            return JsonResponse({"error": "region_id values must be unique"}, status=400)
        seen_region_ids.add(region_id)

        region_normalized = bool(region_raw.get("normalized", normalized_default))
        points, point_labels, err = _parse_points(
            region_raw.get("points"), region_raw.get("point_labels"), region_normalized, prefix
        )
        if err:
            return JsonResponse({"error": err}, status=400)

        box, err = _parse_box(region_raw.get("box"), region_normalized, prefix)
        if err:
            return JsonResponse({"error": err}, status=400)

        mask_b64, mask_shape, mask_encoding, err = _parse_mask(
            region_raw.get("mask_b64"),
            region_raw.get("mask_shape"),
            region_raw.get("mask_encoding"),
            prefix,
        )
        if err:
            return JsonResponse({"error": err}, status=400)

        if points is None and box is None and mask_b64 is None:
            return JsonResponse(
                {"error": f"{prefix}must include at least one prompt: points, box, or mask"},
                status=400,
            )

        payload_region = {
            "region_id": region_id,
            "class_name": str(region_raw.get("class_name") or "unknown"),
            "normalized": region_normalized,
        }
        class_id = region_raw.get("class_id")
        if class_id is not None:
            payload_region["class_id"] = str(class_id)
        if points is not None:
            payload_region["points"] = points
            payload_region["point_labels"] = point_labels
        if box is not None:
            payload_region["box"] = box
        if mask_b64 is not None:
            payload_region["mask_b64"] = mask_b64
            payload_region["mask_encoding"] = mask_encoding
            payload_region["mask_shape"] = mask_shape

        regions_payload.append(payload_region)

    Patient = _patient_model()
    patient = get_object_or_404(Patient, patient_id=patient_id)
    can_view, _ = _patient_permissions(profile, patient)
    if not can_view:
        return JsonResponse({"error": "Permission denied"}, status=403)

    worker_url = _worker_url(request, "/api/session/prompt/", "WORKER_SESSION_PROMPT_URL")
    payload = {
        "video_id": video_id,
        "frame_timestamp": frame_timestamp,
        "regions": regions_payload,
        "window_seconds": window_seconds,
        "normalized": normalized_default,
    }

    try:
        status_code, worker_response = _worker_json_request(worker_url, payload, timeout=20)
    except urllib_error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        logger.error(
            "Worker session-prompt HTTP error %s for laparoscopy patient %s: %s",
            exc.code,
            patient.patient_id,
            error_body,
        )
        return JsonResponse(
            {
                "error": "Worker session prompt request failed",
                "worker_status": exc.code,
                "worker_response": error_body,
            },
            status=502,
        )
    except urllib_error.URLError as exc:
        logger.error(
            "Worker session-prompt network error for laparoscopy patient %s: %s",
            patient.patient_id,
            exc,
        )
        return JsonResponse(
            {"error": "Worker service unavailable", "details": str(exc)},
            status=502,
        )

    return JsonResponse(
        {"success": True, "worker_status": status_code, "worker_response": worker_response}
    )
