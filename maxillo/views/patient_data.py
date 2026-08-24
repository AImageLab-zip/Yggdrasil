"""Patient data API endpoints for serving scan data."""

from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.db import transaction
import os
import logging
import tempfile
import hashlib
import json
import math
import re
import io
import uuid
from PIL import Image

from common.annotation_lock import annotation_lock_reasons, lock_message
from common.file_access import exists as artifact_exists, streaming_response
from common.file_access import open_binary
from common.permissions import (
    project_allows_annotation,
    user_can_read_folder,
    user_can_write_annotations,
    user_is_project_admin,
)
from common.object_storage import get_object_storage
from common.models import FileRegistry, Modality
from .domain import get_domain_models
from .patient_detail import _resolved_cbct_viewer_source
from ..models import PanoramicState

logger = logging.getLogger(__name__)

PANORAMIC_VARIANTS = {
    "zplus40_mean": ("panoramic_zplus40_mean_png", "Z+40 Average"),
    "zplus40_raysum": ("panoramic_zplus40_raysum_png", "Z+40 X-ray"),
    "zplus20_mean": ("panoramic_zplus20_mean_png", "Z+20 Average"),
    "zplus20_raysum": ("panoramic_zplus20_raysum_png", "Z+20 X-ray"),
    "z0_mean": ("panoramic_png", "Z+0 Average"),
    "z0_raysum": ("panoramic_z0_raysum_png", "Z+0 X-ray"),
    "zminus20_mean": ("panoramic_zminus20_mean_png", "Z-20 Average"),
    "zminus20_raysum": ("panoramic_zminus20_raysum_png", "Z-20 X-ray"),
    "zminus40_mean": ("panoramic_zminus40_mean_png", "Z-40 Average"),
    "zminus40_raysum": ("panoramic_zminus40_raysum_png", "Z-40 X-ray"),
}
DEFAULT_PANORAMIC_VARIANT = "z0_mean"
BROWSER_PANORAMIC_ALGORITHM = "panorex-js-v2-mip"
BROWSER_PANORAMIC_MAX_REQUEST = 25 * 1024 * 1024
BROWSER_PANORAMIC_MAX_PNG = 10 * 1024 * 1024
BROWSER_PANORAMIC_MAX_STATE = 64 * 1024
BROWSER_PANORAMIC_MAX_PIXELS = 32_000_000

LANDMARK_KEY_RE = re.compile(r"^(\d+)_(upper|lower)_FDI_(\d{2})$")
WORKER_LANDMARK_KEY_RE = re.compile(r"^in_(upper|lower)_FDI_(\d{2})$")
LANDMARK_POINT_TYPES = {
    "incisal", "outer", "bracket", "gingival", "mesial", "distal", "inner", "facial",
}
LANDMARK_MULTI_POINT_TYPES = {"cusps", "planar"}
LANDMARK_PLANE_KEYS = {"origin", "xAxis", "yAxis", "zAxis"}


def _generated_panoramic_variants(panoramic_file):
    metadata = panoramic_file.metadata if isinstance(panoramic_file.metadata, dict) else {}
    if metadata.get("generated_from") != "cbct_to_panoramic":
        return {}
    files = metadata.get("files")
    if not isinstance(files, dict):
        return {}

    variants = {}
    for variant, (output_key, label) in PANORAMIC_VARIANTS.items():
        output = files.get(output_key)
        path = output.get("path") if isinstance(output, dict) else None
        if path:
            variants[variant] = {"path": path, "label": label}
    return variants


def _source_descriptor(source):
    segmentation = source["segmentation_file"]
    return {
        "job_id": source["job"].id if source["job"] else None,
        "file_id": source["file"].id,
        "file_key": source["file_key"],
        "file_hash": source["file_hash"],
        "segmentation_file_id": segmentation.id if segmentation else None,
        "segmentation_file_key": source["segmentation_key"] or None,
        "segmentation_file_hash": source["segmentation_hash"] or None,
    }


def _source_artifact_path(source, file_name, key_name):
    file_obj = source.get(file_name) if source else None
    file_key = source.get(key_name) if source else None
    if not file_obj:
        return None
    if not file_key or file_key == "primary":
        return file_obj.file_path
    metadata = file_obj.metadata if isinstance(file_obj.metadata, dict) else {}
    files = metadata.get("files")
    output = files.get(file_key) if isinstance(files, dict) else None
    return output.get("path") if isinstance(output, dict) else None


def _string_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _string_leaves(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _string_leaves(nested)


def _legacy_panoramic_matches_source(file_obj, active_source):
    metadata = file_obj.metadata if isinstance(file_obj.metadata, dict) else {}
    if metadata.get("generated_from") != "cbct_to_panoramic" or not active_source:
        return True
    inputs = set(_string_leaves(metadata.get("input_files") or {}))
    volume_path = _source_artifact_path(active_source, "file", "file_key")
    segmentation_path = _source_artifact_path(
        active_source, "segmentation_file", "segmentation_key"
    )
    if not volume_path or volume_path not in inputs:
        return False
    return not segmentation_path or segmentation_path in inputs


def _state_matches_source(state, source):
    if not state or not source:
        return False
    descriptor = _source_descriptor(source)
    return (
        state.algorithm_version == BROWSER_PANORAMIC_ALGORITHM
        and state.source_job_id == descriptor["job_id"]
        and state.source_file_id == descriptor["file_id"]
        and state.source_file_key == descriptor["file_key"]
        and state.source_file_hash == descriptor["file_hash"]
        and state.source_segmentation_file_id == descriptor["segmentation_file_id"]
        and (state.source_segmentation_key or None) == descriptor["segmentation_file_key"]
        and (state.source_segmentation_hash or None) == descriptor["segmentation_file_hash"]
    )


def _source_shape(source):
    row = source["file"]
    metadata = row.metadata if isinstance(row.metadata, dict) else {}
    candidates = [metadata.get("volume_shape"), metadata.get("shape")]
    files = metadata.get("files")
    if isinstance(files, dict):
        output = files.get(source["file_key"])
        if isinstance(output, dict):
            candidates.extend((output.get("volume_shape"), output.get("shape")))
    for candidate in candidates:
        if (
            isinstance(candidate, (list, tuple))
            and len(candidate) == 3
            and all(isinstance(value, int) and not isinstance(value, bool) for value in candidate)
        ):
            return list(candidate)
    return None


def _input_value(data, snake_name, camel_name=None):
    if snake_name in data:
        return data[snake_name]
    if camel_name and camel_name in data:
        return data[camel_name]
    return None


def _source_input_value(data, *names):
    for name in names:
        if name in data:
            return data[name]
    return None


def _normalize_browser_panoramic_state(payload, active_source):
    if not isinstance(payload, dict):
        raise ValueError("state must be a JSON object")
    source_payload = payload.get("source")
    if not isinstance(source_payload, dict):
        raise ValueError("state.source must be an object")

    source = {
        "job_id": _input_value(source_payload, "job_id", "jobId"),
        "file_id": _source_input_value(
            source_payload, "file_id", "fileId", "volume_file_id", "volumeFileId"
        ),
        "file_key": _source_input_value(
            source_payload, "file_key", "fileKey", "volume_file_key", "volumeFileKey"
        ),
        "file_hash": _source_input_value(
            source_payload, "file_hash", "fileHash", "volume_file_hash", "volumeFileHash"
        ),
        "segmentation_file_id": _input_value(
            source_payload, "segmentation_file_id", "segmentationFileId"
        ),
        "segmentation_file_key": _input_value(
            source_payload, "segmentation_file_key", "segmentationFileKey"
        ),
        "segmentation_file_hash": _input_value(
            source_payload, "segmentation_file_hash", "segmentationFileHash"
        ),
    }
    expected_source = _source_descriptor(active_source)
    if source != expected_source:
        raise RuntimeError("The active CBCT source has changed")

    shape = _input_value(payload, "volume_shape", "volumeShape")
    if (
        not isinstance(shape, list)
        or len(shape) != 3
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 16384
            for value in shape
        )
    ):
        raise ValueError("volume_shape must contain three positive integer dimensions")
    known_shape = _source_shape(active_source)
    if known_shape and shape != known_shape:
        raise RuntimeError("The CBCT volume shape has changed")

    axial_slice = _input_value(payload, "axial_slice", "axialSlice")
    if (
        isinstance(axial_slice, bool)
        or not isinstance(axial_slice, int)
        or axial_slice < 0
        or axial_slice >= shape[2]
    ):
        raise ValueError("axial_slice is outside the CBCT volume")

    spline = payload.get("spline")
    if isinstance(spline, dict):
        control_points = _input_value(spline, "control_points", "controlPoints")
    else:
        control_points = spline
    if not isinstance(control_points, list) or not 4 <= len(control_points) <= 64:
        raise ValueError("spline must contain between 4 and 64 control points")
    normalized_points = []
    for point in control_points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("Each spline control point must be [x, y]")
        coordinates = []
        for axis, coordinate in enumerate(point):
            if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
                raise ValueError("Spline coordinates must be numeric")
            coordinate = float(coordinate)
            if not math.isfinite(coordinate) or coordinate < 0 or coordinate >= shape[axis]:
                raise ValueError("Spline control points must be finite and inside the volume")
            coordinates.append(coordinate)
        normalized_points.append(coordinates)

    mode = _input_value(payload, "default_mode", "defaultMode")
    if mode not in {"mip", "raysum"}:
        raise ValueError("default_mode must be mip or raysum")
    geometry_source = _input_value(payload, "geometry_source", "geometrySource")
    if geometry_source not in {"auto", "custom_cp"}:
        raise ValueError("geometry_source must be auto or custom_cp")
    algorithm = _input_value(payload, "algorithm_version", "algorithmVersion")
    if algorithm != BROWSER_PANORAMIC_ALGORITHM:
        raise ValueError("Unsupported algorithm_version")
    generation_value = _input_value(payload, "generation_uuid", "generationUuid")
    try:
        generation_uuid = uuid.UUID(str(generation_value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("generation_uuid must be a valid UUID")
    base_revision = _input_value(payload, "base_revision", "baseRevision")
    if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 0:
        raise ValueError("base_revision must be a non-negative integer")

    return {
        "source": source,
        "volume_shape": shape,
        "axial_slice": axial_slice,
        "spline": normalized_points,
        "geometry_source": geometry_source,
        "default_mode": mode,
        "algorithm_version": algorithm,
        "generation_uuid": generation_uuid,
        "base_revision": base_revision,
    }


def _sanitize_browser_png(uploaded, label):
    if not uploaded:
        raise ValueError(f"{label}_png is required")
    if uploaded.size <= 0 or uploaded.size > BROWSER_PANORAMIC_MAX_PNG:
        raise ValueError(f"{label}_png exceeds the allowed size")
    raw = uploaded.read(BROWSER_PANORAMIC_MAX_PNG + 1)
    if len(raw) > BROWSER_PANORAMIC_MAX_PNG or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"{label}_png is not a valid PNG")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                raise ValueError
            width, height = image.size
            if width < 1 or height < 1 or width > 16384 or height > 16384:
                raise ValueError
            if width * height > BROWSER_PANORAMIC_MAX_PIXELS:
                raise ValueError
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            if image.mode not in {"L", "LA", "RGB", "RGBA"}:
                raise ValueError
            output = io.BytesIO()
            image.save(output, format="PNG")
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ValueError(f"{label}_png is not a supported single-frame PNG")
    encoded = output.getvalue()
    if len(encoded) > BROWSER_PANORAMIC_MAX_PNG:
        raise ValueError(f"{label}_png exceeds the allowed size after sanitization")
    return encoded, (width, height), hashlib.sha256(encoded).hexdigest()


def _upload_panoramic_bytes(storage, content, key):
    fd, path = tempfile.mkstemp(prefix="browser_panorex_", suffix=".png")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
        storage.upload_file(path, key=key, content_type="image/png")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _panoramic_save_response(state, *, idempotent=False):
    variants = [
        {"id": "mip", "label": "MIP"},
        {"id": "raysum", "label": "X-ray"},
    ]
    return JsonResponse({
        "success": True,
        "revision": state.revision,
        "generation_uuid": str(state.generation_uuid),
        "default_mode": state.default_mode,
        "selected_variant": state.default_mode,
        "variants": variants,
        "idempotent": idempotent,
    })


@login_required
@require_POST
def save_browser_panoramic(request, patient_id):
    """Validate and persist browser-generated panoramic PNGs without dispatching work."""
    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_write_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)

    # Once annotations exist the panoramic is part of a record that has to stay
    # reproducible, so it stops being editable. A patient annotated before any
    # panoramic was produced would otherwise be left without one forever, so the
    # silent automatic default is still allowed exactly once; anything the user
    # drew by hand, and any replacement of an existing arch, is refused. The lock
    # ignores this patient's own panoramic state, or the first edit would be the
    # last one allowed.
    lock_reasons = annotation_lock_reasons(patient, include_panoramic=False)
    if lock_reasons and PanoramicState.objects.filter(patient=patient).exists():
        return JsonResponse(
            {"error": lock_message(lock_reasons, subject="panoramic arch"), "panoramic_locked": True},
            status=409,
        )

    try:
        content_length = int(request.META.get("CONTENT_LENGTH", ""))
    except (TypeError, ValueError):
        content_length = 0
    if content_length <= 0 or content_length > BROWSER_PANORAMIC_MAX_REQUEST:
        return JsonResponse({"error": "Invalid or excessive Content-Length"}, status=413)

    state_raw = request.POST.get("state")
    if not isinstance(state_raw, str) or len(state_raw.encode("utf-8")) > BROWSER_PANORAMIC_MAX_STATE:
        return JsonResponse({"error": "state is required and must be at most 64 KiB"}, status=400)
    active_source = _resolved_cbct_viewer_source(patient)
    if not active_source:
        return JsonResponse({"error": "No active CBCT source"}, status=409)
    try:
        payload = json.loads(state_raw)
        normalized = _normalize_browser_panoramic_state(payload, active_source)
        mip_bytes, mip_size, mip_hash = _sanitize_browser_png(
            request.FILES.get("mip_png"), "mip"
        )
        raysum_bytes, raysum_size, raysum_hash = _sanitize_browser_png(
            request.FILES.get("raysum_png"), "raysum"
        )
        if mip_size != raysum_size:
            raise ValueError("mip_png and raysum_png dimensions must match")
        if mip_size[1] != normalized["volume_shape"][2]:
            raise ValueError("Panoramic image height must match the CBCT depth")
    except json.JSONDecodeError:
        return JsonResponse({"error": "state is not valid JSON"}, status=400)
    except RuntimeError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    if lock_reasons and normalized["geometry_source"] != "auto":
        return JsonResponse(
            {"error": lock_message(lock_reasons, subject="panoramic arch"), "panoramic_locked": True},
            status=409,
        )

    fingerprint_data = dict(normalized)
    fingerprint_data["generation_uuid"] = str(normalized["generation_uuid"])
    fingerprint_data["image_hashes"] = [mip_hash, raysum_hash]
    request_hash = hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    current = PanoramicState.objects.filter(patient=patient).first()
    if current and current.generation_uuid == normalized["generation_uuid"]:
        if current.request_hash == request_hash:
            return _panoramic_save_response(current, idempotent=True)
        return JsonResponse({"error": "generation_uuid was already used"}, status=409)
    current_matches_source = _state_matches_source(current, active_source)
    effective_revision = current.revision if current_matches_source else 0
    if effective_revision != normalized["base_revision"]:
        return JsonResponse({"error": "Stale panoramic revision"}, status=409)

    generation = str(normalized["generation_uuid"])
    prefix = f"maxillo/processed/panoramic/patient_{patient.patient_id}/{generation}"
    keys = {"mip": f"{prefix}/mip.png", "raysum": f"{prefix}/raysum.png"}
    storage = None
    uploaded_keys = []
    idempotent_state = None
    try:
        storage = get_object_storage()
        with transaction.atomic():
            locked_patient = Patient.objects.select_for_update().get(pk=patient.pk)
            locked_source = _resolved_cbct_viewer_source(locked_patient)
            if not locked_source or _source_descriptor(locked_source) != normalized["source"]:
                raise RuntimeError("The active CBCT source has changed")
            current = PanoramicState.objects.select_for_update().filter(patient=locked_patient).first()
            if current and current.generation_uuid == normalized["generation_uuid"]:
                if current.request_hash == request_hash:
                    idempotent_state = current
                else:
                    raise RuntimeError("generation_uuid was already used")
            else:
                idempotent_state = None
            current_matches_source = _state_matches_source(current, locked_source)
            effective_revision = current.revision if current_matches_source else 0
            if idempotent_state is None and effective_revision != normalized["base_revision"]:
                raise RuntimeError("Stale panoramic revision")
            if idempotent_state is not None:
                state = idempotent_state
            else:
                # The patient row lock serializes uploads for one case, preventing two
                # concurrent requests from overwriting or deleting the same UUID keys.
                uploaded_keys.append(keys["mip"])
                _upload_panoramic_bytes(storage, mip_bytes, keys["mip"])
                uploaded_keys.append(keys["raysum"])
                _upload_panoramic_bytes(storage, raysum_bytes, keys["raysum"])

                revision = effective_revision + 1
                source_metadata = dict(normalized["source"])
                common_metadata = {
                    "generated_from": "browser_cbct_to_panoramic",
                    "generation_uuid": generation,
                    "source": source_metadata,
                    "annotation": {
                        "axial_slice": normalized["axial_slice"],
                        "volume_shape": normalized["volume_shape"],
                        "spline": normalized["spline"],
                        "geometry_source": normalized["geometry_source"],
                        "algorithm_version": normalized["algorithm_version"],
                        "revision": revision,
                    },
                    "image_width": mip_size[0],
                    "image_height": mip_size[1],
                    "interpolation": "bilinear",
                    "slab": {
                        "half_width_voxels": 20,
                        "intervals": 40,
                        "sample_count": 41,
                    },
                    "generated_by": request.user.username,
                }
                modality = Modality.objects.filter(slug="panoramic").first()
                new_rows = []
                for variant, content, digest in (
                    ("mip", mip_bytes, mip_hash),
                    ("raysum", raysum_bytes, raysum_hash),
                ):
                    metadata = dict(common_metadata)
                    metadata.update({
                        "variant": variant,
                        "projection": "maximum" if variant == "mip" else "nonnegative_ray_sum",
                        "is_default": variant == normalized["default_mode"],
                    })
                    new_rows.append(FileRegistry.objects.create(
                        file_type="panoramic_processed",
                        subtype=variant,
                        file_path=keys[variant],
                        file_size=len(content),
                        file_hash=digest,
                        metadata=metadata,
                        modality=modality,
                        domain="maxillo",
                        patient=locked_patient,
                        processing_job=None,
                    ))
                old_rows = []
                if current:
                    old_rows = [row for row in (current.mip_file, current.raysum_file) if row]
                    state = current
                else:
                    state = PanoramicState(patient=locked_patient)
                state.source_job = locked_source["job"]
                state.source_file = locked_source["file"]
                state.source_file_key = locked_source["file_key"]
                state.source_file_hash = locked_source["file_hash"]
                state.source_segmentation_file = locked_source["segmentation_file"]
                state.source_segmentation_key = locked_source["segmentation_key"]
                state.source_segmentation_hash = locked_source["segmentation_hash"]
                state.mip_file, state.raysum_file = new_rows
                state.axial_slice = normalized["axial_slice"]
                state.volume_shape = normalized["volume_shape"]
                state.spline = normalized["spline"]
                state.geometry_source = normalized["geometry_source"]
                state.default_mode = normalized["default_mode"]
                state.algorithm_version = normalized["algorithm_version"]
                state.revision = revision
                state.generation_uuid = normalized["generation_uuid"]
                state.request_hash = request_hash
                state.generated_by = request.user
                state.save()

                old_owned = [
                    row for row in old_rows
                    if isinstance(row.metadata, dict)
                    and row.metadata.get("generated_from") == "browser_cbct_to_panoramic"
                ]
                old_ids = [row.id for row in old_owned]
                old_paths = [row.file_path for row in old_owned]
                if old_ids:
                    FileRegistry.objects.filter(id__in=old_ids).delete()

                def cleanup_old_outputs():
                    for old_path in old_paths:
                        try:
                            storage.delete(old_path)
                        except Exception:
                            logger.warning("Unable to delete old browser panoramic %s", old_path)

                transaction.on_commit(cleanup_old_outputs, robust=True)
    except RuntimeError as exc:
        for key in uploaded_keys:
            try:
                if storage:
                    storage.delete(key)
            except Exception:
                logger.warning("Unable to clean rejected browser panoramic %s", key)
        return JsonResponse({"error": str(exc)}, status=409)
    except Exception:
        for key in uploaded_keys:
            try:
                if storage:
                    storage.delete(key)
            except Exception:
                logger.warning("Unable to clean failed browser panoramic %s", key)
        logger.exception("Unable to save browser panoramic for patient %s", patient.patient_id)
        return JsonResponse({"error": "Unable to save panoramic"}, status=500)

    return _panoramic_save_response(state, idempotent=bool(idempotent_state))


def _normalize_landmark_point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("Landmark points must be [x, y, z].")
    normalized = []
    for coordinate in value:
        if isinstance(coordinate, bool):
            raise ValueError("Landmark coordinates must be numeric.")
        try:
            coordinate = float(coordinate)
        except (TypeError, ValueError):
            raise ValueError("Landmark coordinates must be numeric.")
        if not math.isfinite(coordinate):
            raise ValueError("Landmark coordinates must be finite.")
        normalized.append(coordinate)
    return normalized


def _normalize_landmarks_payload(payload, patient_id):
    if not isinstance(payload, dict):
        raise ValueError("Landmarks must be a JSON object.")

    normalized = {}
    for entry_key, entry in payload.items():
        match = LANDMARK_KEY_RE.match(str(entry_key))
        if not match or match.group(1) != str(patient_id):
            raise ValueError("Landmark keys must belong to this patient and use the FDI format.")
        if not isinstance(entry, dict):
            raise ValueError("Each tooth landmark entry must be an object.")

        jaw, tooth = match.group(2), match.group(3)
        if tooth[0] not in "1234" or tooth[1] not in "12345678":
            raise ValueError("Landmark tooth codes must be permanent-dentition FDI codes.")
        if (tooth[0] in "12") != (jaw == "upper"):
            raise ValueError("Landmark jaw and FDI tooth code do not match.")

        normalized_entry = dict(entry)
        for landmark_type in LANDMARK_POINT_TYPES:
            if landmark_type in entry:
                normalized_entry[landmark_type] = _normalize_landmark_point(entry[landmark_type])
        for landmark_type in LANDMARK_MULTI_POINT_TYPES:
            if landmark_type not in entry:
                continue
            points = entry[landmark_type]
            if not isinstance(points, list) or len(points) > 500:
                raise ValueError(f"{landmark_type} must contain at most 500 points.")
            normalized_entry[landmark_type] = [
                _normalize_landmark_point(point) for point in points
            ]
        if "basePlane" in entry:
            plane = entry["basePlane"]
            if not isinstance(plane, dict) or set(plane) != LANDMARK_PLANE_KEYS:
                raise ValueError("basePlane must include origin, xAxis, yAxis, and zAxis.")
            normalized_entry["basePlane"] = {
                name: _normalize_landmark_point(plane[name])
                for name in LANDMARK_PLANE_KEYS
            }
        normalized[str(entry_key)] = normalized_entry
    return normalized


def _normalize_loaded_landmarks(payload, patient_id):
    """Accept worker wrappers/aggregate files and return this patient's canonical document."""
    if isinstance(payload, dict) and isinstance(payload.get("landmarks"), dict):
        payload = payload["landmarks"]
    if not isinstance(payload, dict):
        raise ValueError("Landmarks must be a JSON object.")

    patient_landmarks = {}
    for key, entry in payload.items():
        match = LANDMARK_KEY_RE.match(str(key))
        if match and int(match.group(1)) == int(patient_id):
            jaw, tooth = match.group(2), match.group(3)
        else:
            worker_match = WORKER_LANDMARK_KEY_RE.match(str(key))
            if not worker_match:
                continue
            jaw, tooth = worker_match.group(1), worker_match.group(2)
        canonical_key = f"{patient_id}_{jaw}_FDI_{tooth}"
        patient_landmarks[canonical_key] = entry
    return _normalize_landmarks_payload(patient_landmarks, patient_id)


def _serve_file_url(request, file_id):
    namespace = (
        getattr(request, "resolver_match", None) and request.resolver_match.namespace
    ) or "maxillo"
    return reverse(f"{namespace}:api_serve_file", kwargs={"file_id": file_id})


def _can_read_patient(request, patient):
    if user_is_project_admin(request.user, request):
        return True
    if not patient.folder:
        return False
    return user_can_read_folder(request.user, patient.folder, request)


def _can_write_patient(request, patient):
    if user_is_project_admin(request.user, request):
        return True
    return bool(
        patient.folder and user_can_write_annotations(request.user, patient.folder, request)
    )


def _active_ios_landmarks(patient):
    return patient.files.filter(file_type="ios_landmarks").order_by("-created_at", "-id").first()


@login_required
def patient_ios_landmarks(request, patient_id):
    """Load or explicitly save the canonical patient IOS landmarks document."""
    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)

    if request.method == "GET":
        if not _can_read_patient(request, patient):
            return JsonResponse({"error": "Permission denied"}, status=403)
        landmark_file = _active_ios_landmarks(patient)
        if not landmark_file:
            return JsonResponse({"landmarks": {}, "file_id": None, "source": None})
        try:
            body, _ = open_binary(landmark_file.file_path)
            try:
                landmarks = _normalize_loaded_landmarks(
                    json.loads(body.read().decode("utf-8")), patient.patient_id
                )
            finally:
                body.close()
        except Exception:
            logger.exception("Unable to load IOS landmarks for patient %s", patient.patient_id)
            return JsonResponse({"error": "Landmark file could not be loaded"}, status=500)
        return JsonResponse({
            "landmarks": landmarks,
            "file_id": landmark_file.id,
            "source": (landmark_file.metadata or {}).get("origin"),
        })

    if request.method != "PUT":
        return JsonResponse({"error": "Method not allowed"}, status=405)
    if not _can_write_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)
    if not project_allows_annotation(patient, "ios_landmarks"):
        return JsonResponse({"error": "IOS landmarks are disabled for this project"}, status=403)
    try:
        body = json.loads(request.body.decode("utf-8"))
        landmarks = body.get("landmarks") if isinstance(body, dict) and "landmarks" in body else body
        landmarks = _normalize_landmarks_payload(landmarks, patient.patient_id)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return JsonResponse({"error": str(exc) or "Invalid landmarks payload"}, status=400)

    encoded = json.dumps(landmarks, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    object_key = f"maxillo/processed/ios/ios_landmarks_patient_{patient.patient_id}.json"
    fd, tmp_path = tempfile.mkstemp(prefix="ios_landmarks_", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as temp_file:
            temp_file.write(encoded)
        get_object_storage().upload_file(tmp_path, key=object_key)
    except Exception:
        logger.exception("Unable to save IOS landmarks for patient %s", patient.patient_id)
        return JsonResponse({"error": "Unable to save landmarks"}, status=500)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    now = timezone.now()
    metadata = {
        "origin": "manual",
        "updated_by": request.user.username,
        "updated_at": now.isoformat(),
        "schema_version": 1,
    }
    landmark_file = _active_ios_landmarks(patient)
    if landmark_file:
        landmark_file.file_path = object_key
        landmark_file.file_size = len(encoded)
        landmark_file.file_hash = hashlib.sha256(encoded).hexdigest()
        landmark_file.metadata = metadata
        landmark_file.processing_job = None
        landmark_file.modality = Modality.objects.filter(slug="ios").first()
        landmark_file.save()
    else:
        landmark_file = FileRegistry.objects.create(
            file_type="ios_landmarks",
            file_path=object_key,
            file_size=len(encoded),
            file_hash=hashlib.sha256(encoded).hexdigest(),
            metadata=metadata,
            modality=Modality.objects.filter(slug="ios").first(),
            domain="maxillo",
            patient=patient,
        )
    return JsonResponse({"success": True, "file_id": landmark_file.id, "updated_at": now.isoformat()})


def _content_type_for_image_path(file_path):
    ext = os.path.splitext((file_path or "").lower())[1]
    if ext in [".jpg", ".jpeg"]:
        return "image/jpeg"
    if ext == ".gif":
        return "image/gif"
    if ext == ".webp":
        return "image/webp"
    return "image/png"


def _latest_official_image_file(patient, file_types, *, source_file_id=None, image_index=None):
    qs = patient.files.filter(file_type__in=file_types)
    if source_file_id is not None:
        qs = qs.filter(metadata__source_file_id=source_file_id)
    elif image_index is not None:
        qs = qs.filter(metadata__image_index=image_index)
    return qs.order_by("-created_at", "-id").first()


@login_required
def patient_viewer_data(request, patient_id):
    """API endpoint to provide scan data for 3D viewer"""
    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_read_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)

    # Determine modality status using Jobs (use 'ios' modality slug from request context or default)
    modality_slug = "ios"  # This endpoint specifically serves IOS data
    try:
        from common.models import Job as _Job

        job_filter = {
            "domain": "maxillo",
            "modality_slug": modality_slug,
            "status": "processing",
            "patient_id": patient.patient_id,
        }
        if _Job.objects.filter(**job_filter).exists():
            return JsonResponse(
                {
                    "error": f"{modality_slug.upper()} scans are still being processed",
                    "status": "processing",
                    "message": "The scans are being processed. This may take a few minutes.",
                },
                status=202,
            )
        failed_filter = {
            "domain": "maxillo",
            "modality_slug": modality_slug,
            "status": "failed",
            "patient_id": patient.patient_id,
        }
        if _Job.objects.filter(**failed_filter).exists():
            return JsonResponse(
                {
                    "error": f"{modality_slug.upper()} processing failed",
                    "status": "failed",
                    "message": "The scan processing failed. Please try uploading again or contact support.",
                },
                status=500,
            )
    except Exception:
        pass

    # Try to get scan URLs from FileRegistry
    upper_scan_url = None
    lower_scan_url = None

    # Select one complete pair according to the root IOS step's viewer policy.
    try:
        from common.modality_config import (
            modality_prefers_processed_for_viewer,
            raw_file_hidden,
        )

        processed_files = patient.get_ios_processed_files()
        raw_files = patient.get_ios_raw_files()

        processed_pair = None
        if processed_files["upper"] and processed_files["lower"]:
            processed_pair = (processed_files["upper"], processed_files["lower"])

        raw_pair = None
        if raw_files["upper"] and raw_files["lower"]:
            candidate = (raw_files["upper"], raw_files["lower"])
            if not any(raw_file_hidden(file_obj) for file_obj in candidate):
                raw_pair = candidate

        pairs = (
            (processed_pair, raw_pair)
            if modality_prefers_processed_for_viewer(modality_slug)
            else (raw_pair, processed_pair)
        )
        selected_pair = next((pair for pair in pairs if pair is not None), None)
        if selected_pair:
            upper_scan_url = _serve_file_url(request, selected_pair[0].id)
            lower_scan_url = _serve_file_url(request, selected_pair[1].id)
    except Exception:
        pass

    if not upper_scan_url or not lower_scan_url:
        return JsonResponse(
            {"error": "No IOS scan data available", "status": "not_found"}, status=404
        )

    # Ensure URLs use HTTPS if the request came over HTTPS
    def build_secure_uri(request, url):
        # Check if request is secure (either direct HTTPS or behind proxy)
        is_secure = (
            request.is_secure() or request.META.get("HTTP_X_FORWARDED_PROTO") == "https"
        )

        # Always use HTTPS if the request is secure, regardless of the original URL
        if is_secure:
            if url.startswith("/"):
                # Relative URL - build absolute URL with HTTPS
                return f"https://{request.get_host()}{url}"
            elif url.startswith("http://"):
                # HTTP URL - convert to HTTPS
                return url.replace("http://", "https://", 1)
            elif url.startswith("https://"):
                # Already HTTPS - return as-is
                return url
            else:
                # Any other case - assume it's a relative URL and make it HTTPS
                return f"https://{request.get_host()}/{url.lstrip('/')}"
        else:
            # For non-secure requests, use standard build_absolute_uri
            return request.build_absolute_uri(url)

    is_secure = (
        request.is_secure() or request.META.get("HTTP_X_FORWARDED_PROTO") == "https"
    )
    logger.debug(
        f"Request secure: {request.is_secure()}, X-Forwarded-Proto: {request.META.get('HTTP_X_FORWARDED_PROTO')}, is_secure: {is_secure}"
    )
    logger.debug(f"Original URLs - upper: {upper_scan_url}, lower: {lower_scan_url}")

    upper_url = build_secure_uri(request, upper_scan_url)
    lower_url = build_secure_uri(request, lower_scan_url)

    logger.debug(f"Final URLs - upper: {upper_url}, lower: {lower_url}")

    data = {
        "upper_scan_url": upper_url,
        "lower_scan_url": lower_url,
        "patient_info": {
            "patient_id": patient.patient_id,
        },
    }

    return JsonResponse(data)


@login_required
def patient_cbct_data(request, patient_id):
    """API endpoint to serve CBCT data"""
    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_read_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)

    # Determine modality status using Jobs (use 'cbct' modality slug for this endpoint)
    modality_slug = "cbct"  # This endpoint specifically serves CBCT data
    try:
        from common.models import Job as _Job

        job_filter = {
            "domain": "maxillo",
            "modality_slug": modality_slug,
            "status": "processing",
            "patient_id": patient.patient_id,
        }
        if _Job.objects.filter(**job_filter).exists():
            return JsonResponse(
                {
                    "error": f"{modality_slug.upper()} is still being processed",
                    "status": "processing",
                    "message": "The volume is being converted to NIfTI format. This may take a few minutes.",
                },
                status=202,
            )
        failed_filter = {
            "domain": "maxillo",
            "modality_slug": modality_slug,
            "status": "failed",
            "patient_id": patient.patient_id,
        }
        if _Job.objects.filter(**failed_filter).exists():
            return JsonResponse(
                {
                    "error": f"{modality_slug.upper()} processing failed",
                    "status": "failed",
                    "message": "The volume processing failed. Please try uploading again or contact support.",
                },
                status=500,
            )
    except Exception:
        pass

    # Get CBCT file path from raw NIfTI uploads.
    file_path = None

    # Use raw CBCT if available.
    if not file_path:
        try:
            # Do not rely on get_cbct_raw_file() because legacy data may contain
            # multiple cbct_raw rows (including non-NIfTI files).
            raw_entries = patient.files.filter(file_type="cbct_raw").order_by(
                "-created_at"
            )
            for raw_entry in raw_entries:
                raw_path = raw_entry.file_path
                if not raw_path:
                    continue
                if (
                    raw_path.endswith(".nii") or raw_path.endswith(".nii.gz")
                ) and artifact_exists(raw_path):
                    file_path = raw_path
                    break
        except Exception:
            pass

    if not file_path or not artifact_exists(file_path):
        return JsonResponse(
            {"error": "No CBCT data available", "status": "not_found"}, status=404
        )

    try:
        return streaming_response(
            path_or_key=file_path,
            content_type="application/octet-stream",
            filename=f"cbct_{patient_id}.nii.gz",
            as_attachment=True,
        )

    except Exception as e:
        logger.error(f"Error serving CBCT data: {e}", exc_info=True)
        return JsonResponse(
            {"error": f"Failed to load CBCT data: {str(e)}"}, status=500
        )


@login_required
def patient_volume_data(request, patient_id, modality_slug):
    """Generic API endpoint to serve NIfTI volume for arbitrary modality (no panoramic).

    Strategy:
    - Use latest FileRegistry entry for (patient, modality) that endswith .nii or .nii.gz
    """
    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_read_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)
    try:
        from common.models import FileRegistry as _FR
    except Exception:
        return JsonResponse({"error": "File registry unavailable"}, status=500)
    file_path = None
    try:
        processed_filter = {
            "domain": "maxillo",
            "modality__slug": modality_slug,
            "file_type": "cbct_processed",
            "patient_id": patient.patient_id,
        }
        processed = _FR.objects.filter(**processed_filter).first()
        if (
            processed
            and processed.file_hash == "multi-file"
            and "files" in processed.metadata
        ):
            files_data = processed.metadata.get("files", {})
            nifti = files_data.get("volume_nifti", {})
            vol_path = nifti.get("path")
            if vol_path and artifact_exists(vol_path):
                file_path = vol_path
    except Exception:
        pass
    # Fallback: use the latest raw NIfTI
    if not file_path:
        try:
            raw_filter = {
                "domain": "maxillo",
                "modality__slug": modality_slug,
                "patient_id": patient.patient_id,
            }
            raw = _FR.objects.filter(**raw_filter).order_by("-created_at").first()
            if (
                raw
                and raw.file_path
                and (
                    raw.file_path.endswith(".nii") or raw.file_path.endswith(".nii.gz")
                )
                and artifact_exists(raw.file_path)
            ):
                file_path = raw.file_path
        except Exception:
            pass
    if not file_path:
        return JsonResponse(
            {"error": f"No volume data for {modality_slug}"}, status=404
        )
    try:
        return streaming_response(
            path_or_key=file_path,
            content_type="application/octet-stream",
            filename=f"{modality_slug}_{patient_id}.nii.gz",
            as_attachment=True,
        )
    except Exception as e:
        return JsonResponse({"error": f"Failed to load volume: {e}"}, status=500)


@login_required
def patient_panoramic_data(request, patient_id):
    """Serve manually uploaded or CBCT-generated panoramic variants."""

    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_read_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)

    active_source = _resolved_cbct_viewer_source(patient)
    state = PanoramicState.objects.select_related("mip_file", "raysum_file").filter(
        patient=patient
    ).first()
    if _state_matches_source(state, active_source):
        browser_variants = {
            "mip": {"file": state.mip_file, "label": "MIP"},
            "raysum": {"file": state.raysum_file, "label": "X-ray"},
        }
        available_variants = {
            variant: data
            for variant, data in browser_variants.items()
            if data["file"] and artifact_exists(data["file"].file_path)
        }
        requested_variant = request.GET.get("variant", "").strip()
        if requested_variant and requested_variant not in available_variants:
            return JsonResponse(
                {"error": "Panoramic variant not available", "status": "not_found"},
                status=404,
            )
        selected_variant = requested_variant
        if not selected_variant and available_variants:
            selected_variant = (
                state.default_mode
                if state.default_mode in available_variants
                else next(iter(available_variants))
            )
        if selected_variant:
            selected_file = available_variants[selected_variant]["file"]
            if request.GET.get("meta") == "1":
                return JsonResponse({
                    "url": f"{request.path}?variant={selected_variant}",
                    "source_file_id": active_source["file"].id,
                    "raw_url": None,
                    "is_processed": True,
                    "editable": False,
                    "selected_variant": selected_variant,
                    "variants": [
                        {"id": variant, "label": data["label"]}
                        for variant, data in available_variants.items()
                    ],
                    "revision": state.revision,
                    "generation_uuid": str(state.generation_uuid),
                })
            return streaming_response(
                path_or_key=selected_file.file_path,
                content_type="image/png",
                filename=f"panoramic_{patient_id}_{selected_variant}.png",
                as_attachment=False,
            )

    try:
        processed_files = list(
            patient.files.filter(file_type="panoramic_processed").order_by(
                "-created_at", "-id"
            )
        )
        legacy_processed_files = [
            file_obj
            for file_obj in processed_files
            if (
                not isinstance(file_obj.metadata, dict)
                or file_obj.metadata.get("generated_from")
                != "browser_cbct_to_panoramic"
            )
            and _legacy_panoramic_matches_source(file_obj, active_source)
        ]
        panoramic_file = next(
            (
                file_obj
                for file_obj in legacy_processed_files
                if not isinstance(file_obj.metadata, dict)
                or file_obj.metadata.get("generated_from") != "cbct_to_panoramic"
            ),
            None,
        )
        if not panoramic_file:
            panoramic_file = next(
                (
                    file_obj
                    for file_obj in legacy_processed_files
                    if isinstance(file_obj.metadata, dict)
                    and file_obj.metadata.get("is_default")
                ),
                None,
            )
        if not panoramic_file and legacy_processed_files:
            panoramic_file = legacy_processed_files[0]
        if not panoramic_file:
            panoramic_file = (
                patient.files.filter(file_type="panoramic_raw")
                .order_by("-created_at", "-id")
                .first()
            )

        # Security gate: never serve a raw input that is discarded or blocked
        # until processing completes.
        from common.modality_config import raw_file_hidden, modality_discard_raw

        if panoramic_file and raw_file_hidden(panoramic_file):
            panoramic_file = None

        if panoramic_file and artifact_exists(panoramic_file.file_path):
            variants = _generated_panoramic_variants(panoramic_file)
            selected_variant = request.GET.get("variant", "").strip()
            selected_path = panoramic_file.file_path
            if selected_variant:
                selected = variants.get(selected_variant)
                if not selected or not artifact_exists(selected["path"]):
                    return JsonResponse(
                        {"error": "Panoramic variant not available", "status": "not_found"},
                        status=404,
                    )
                selected_path = selected["path"]
            elif DEFAULT_PANORAMIC_VARIANT in variants:
                selected_variant = DEFAULT_PANORAMIC_VARIANT
                selected_path = variants[selected_variant]["path"]

            generated = bool(variants)
            source_file_id = (
                (panoramic_file.metadata or {}).get("source_file_id")
                if isinstance(panoramic_file.metadata, dict)
                else None
            )
            source_file_id = source_file_id or panoramic_file.id
            expose_raw = not generated and not modality_discard_raw("panoramic")
            if request.GET.get("meta") == "1":
                image_url = request.path
                if selected_variant:
                    image_url = f"{image_url}?variant={selected_variant}"
                return JsonResponse(
                    {
                        "url": image_url if generated else _serve_file_url(request, panoramic_file.id),
                        "source_file_id": source_file_id,
                        "raw_url": _serve_file_url(request, source_file_id) if expose_raw else None,
                        "is_processed": panoramic_file.file_type.endswith("_processed"),
                        "editable": expose_raw,
                        "selected_variant": selected_variant or None,
                        "variants": [
                            {"id": variant, "label": data["label"]}
                            for variant, data in variants.items()
                        ],
                    }
                )
            logger.debug("Serving panoramic file: %s", selected_path)
            file_ext = os.path.splitext(selected_path)[1].lower()
            content_type = "image/png"
            if file_ext in [".jpg", ".jpeg"]:
                content_type = "image/jpeg"
            elif file_ext == ".gif":
                content_type = "image/gif"
            elif file_ext == ".webp":
                content_type = "image/webp"

            return streaming_response(
                path_or_key=selected_path,
                content_type=content_type,
                filename=f"panoramic_{patient_id}{file_ext}",
                as_attachment=False,
            )
    except Exception as e:
        logger.warning(f"Error checking for uploaded panoramic file: {e}")

    return JsonResponse(
        {"error": "No panoramic modality file available", "status": "not_found"},
        status=404,
    )


@login_required
def patient_intraoral_data(request, patient_id):
    """API endpoint to serve intraoral photographs data"""

    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_read_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)

    # Get intraoral images from FileRegistry
    try:
        raw_files = patient.files.filter(file_type="intraoral_raw").order_by(
            "metadata__image_index", "created_at", "id"
        )

        if not raw_files.exists():
            legacy_files = patient.files.filter(
                file_type__in=["intraoral-photo_processed", "intraoral_processed"]
            ).order_by(
                "metadata__image_index", "created_at", "id"
            )
            if not legacy_files.exists():
                return JsonResponse({"error": "No intraoral photographs found"}, status=404)
            images_data = []
            for fallback_index, file_obj in enumerate(legacy_files, start=1):
                if not artifact_exists(file_obj.file_path):
                    continue
                image_index = (
                    file_obj.metadata.get("image_index", fallback_index)
                    if isinstance(file_obj.metadata, dict)
                    else fallback_index
                )
                images_data.append(
                    {
                        "id": file_obj.id,
                        "source_file_id": file_obj.id,
                        "index": image_index,
                        "original_filename": (
                            file_obj.metadata.get("original_filename", "")
                            if isinstance(file_obj.metadata, dict)
                            else ""
                        ),
                        "is_processed": True,
                        "edit_meta": (
                            file_obj.metadata.get("edit_meta")
                            if isinstance(file_obj.metadata, dict)
                            else None
                        ),
                        "url": _serve_file_url(request, file_obj.id),
                    }
                )
            if not images_data:
                return JsonResponse(
                    {"error": "No intraoral image files found in storage"},
                    status=404,
                )
            return JsonResponse({"images": images_data, "count": len(images_data)})

        images_data = []
        for fallback_index, file_obj in enumerate(raw_files, start=1):
            if artifact_exists(file_obj.file_path):
                image_index = 0
                if isinstance(file_obj.metadata, dict):
                    image_index = file_obj.metadata.get("image_index", 0) or fallback_index
                processed_file = _latest_official_image_file(
                    patient,
                    ["intraoral-photo_processed", "intraoral_processed"],
                    source_file_id=file_obj.id,
                )
                if not processed_file:
                    processed_file = _latest_official_image_file(
                        patient,
                        ["intraoral-photo_processed", "intraoral_processed"],
                        image_index=image_index,
                    )
                official_file = processed_file or file_obj
                images_data.append(
                    {
                        "id": official_file.id,
                        "source_file_id": file_obj.id,
                        "index": image_index,
                        "original_filename": (
                            file_obj.metadata.get("original_filename", "")
                            if isinstance(file_obj.metadata, dict)
                            else ""
                        ),
                        "is_processed": official_file.file_type.endswith("_processed"),
                        "edit_meta": (
                            official_file.metadata.get("edit_meta")
                            if isinstance(official_file.metadata, dict)
                            else None
                        ),
                        "url": _serve_file_url(request, official_file.id),
                    }
                )

        if not images_data:
            return JsonResponse(
                {"error": "No intraoral image files found in storage"},
                status=404,
            )

        return JsonResponse({"images": images_data, "count": len(images_data)})

    except Exception as e:
        logger.error(f"Error serving intraoral data: {e}", exc_info=True)
        return JsonResponse({"error": "Internal server error"}, status=500)


@login_required
def patient_teleradiography_data(request, patient_id):
    """API endpoint to serve teleradiography image data"""

    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_read_patient(request, patient):
        return JsonResponse({"error": "Permission denied"}, status=403)

    # Look for teleradiography file in FileRegistry
    try:
        # Prefer processed file, fallback to raw
        teleradiography_file = (
            patient.files.filter(file_type="teleradiography_processed")
            .order_by("-created_at", "-id")
            .first()
        )

        if not teleradiography_file:
            teleradiography_file = (
                patient.files.filter(file_type="teleradiography_raw")
                .order_by("-created_at", "-id")
                .first()
            )

        # Security gate: never serve a raw input that is discarded or blocked
        # until processing completes.
        from common.modality_config import raw_file_hidden, modality_discard_raw

        if teleradiography_file and raw_file_hidden(teleradiography_file):
            teleradiography_file = None

        if not teleradiography_file:
            return JsonResponse(
                {"error": "Teleradiography image not found"}, status=404
            )

        source_file_id = (
            (teleradiography_file.metadata or {}).get("source_file_id")
            if isinstance(teleradiography_file.metadata, dict)
            else None
        )
        source_file_id = source_file_id or teleradiography_file.id
        expose_raw = not modality_discard_raw("teleradiography")
        if request.GET.get("meta") == "1":
            return JsonResponse(
                {
                    "url": _serve_file_url(request, teleradiography_file.id),
                    "source_file_id": source_file_id,
                    "raw_url": _serve_file_url(request, source_file_id) if expose_raw else None,
                    "is_processed": teleradiography_file.file_type.endswith("_processed"),
                }
            )

        if not artifact_exists(teleradiography_file.file_path):
            return JsonResponse(
                {"error": "Teleradiography image file not found in storage"},
                status=404,
            )

        # Determine content type
        file_ext = os.path.splitext(teleradiography_file.file_path)[1].lower()
        content_type = "image/jpeg" if file_ext in [".jpg", ".jpeg"] else "image/png"

        return streaming_response(
            path_or_key=teleradiography_file.file_path,
            content_type=content_type,
            filename=f"teleradiography_{patient_id}{file_ext}",
            as_attachment=False,
        )

    except Exception as e:
        logger.error(f"Error serving teleradiography data: {e}", exc_info=True)
        return JsonResponse({"error": "Internal server error"}, status=500)


@login_required
@require_POST
def save_rgb_image_edit(request, patient_id):
    Patient = get_domain_models(request)["Patient"]
    patient = get_object_or_404(Patient, patient_id=patient_id)
    if not _can_write_patient(request, patient):
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    modality_slug = (request.POST.get("modality_slug") or "").strip()
    source_file_id = request.POST.get("source_file_id")
    edited_image = request.FILES.get("image")
    edit_meta_raw = request.POST.get("edit_meta") or "{}"

    modality_to_types = {
        "intraoral-photo": ("intraoral_raw", "intraoral-photo_processed"),
        "teleradiography": ("teleradiography_raw", "teleradiography_processed"),
        "panoramic": ("panoramic_raw", "panoramic_processed"),
    }
    if modality_slug not in modality_to_types:
        return JsonResponse({"success": False, "error": "Unsupported modality"}, status=400)
    if not source_file_id:
        return JsonResponse({"success": False, "error": "source_file_id is required"}, status=400)
    try:
        source_file_id = int(source_file_id)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid source_file_id"}, status=400)
    if not edited_image:
        return JsonResponse({"success": False, "error": "Edited image is required"}, status=400)

    try:
        edit_meta = json.loads(edit_meta_raw)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid edit metadata"}, status=400)

    raw_type, processed_type = modality_to_types[modality_slug]
    source_file = get_object_or_404(FileRegistry, id=source_file_id, patient=patient)
    if source_file.file_type != raw_type:
        return JsonResponse({"success": False, "error": "Source file type mismatch"}, status=400)

    ext = os.path.splitext(edited_image.name or "")[1].lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
        ext = ".png"
    now = timezone.now()
    object_key = (
        f"maxillo/processed/{modality_slug}/{modality_slug}_patient_{patient.patient_id}"
        f"_{source_file_id}_{now.strftime('%Y%m%d%H%M%S')}{ext}"
    )

    fd, tmp_path = tempfile.mkstemp(prefix="tf_rgb_edit_", suffix=ext)
    os.close(fd)
    hash_sha256 = hashlib.sha256()
    file_size = 0
    output_width = None
    output_height = None
    try:
        with open(tmp_path, "wb+") as destination:
            for chunk in edited_image.chunks():
                destination.write(chunk)
                hash_sha256.update(chunk)
                file_size += len(chunk)
        with Image.open(tmp_path) as saved_image:
            output_width, output_height = saved_image.size
        get_object_storage().upload_file(tmp_path, key=object_key)
    except Exception as exc:
        logger.error("Failed to store processed RGB image: %s", exc, exc_info=True)
        return JsonResponse({"success": False, "error": "Failed to save processed image"}, status=500)
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    # Keep one processed file per source image (replace older rows + files)
    old_entries = patient.files.filter(
        file_type=processed_type,
        metadata__source_file_id=source_file_id,
    )
    for row in old_entries:
        try:
            get_object_storage().delete(row.file_path)
        except Exception:
            logger.warning("Failed deleting old processed object %s", row.file_path)
    old_entries.delete()

    modality_fk = Modality.objects.filter(slug=modality_slug).first()
    metadata = dict(source_file.metadata or {})
    if output_width and output_height:
        metadata["image_width"] = output_width
        metadata["image_height"] = output_height
    metadata.update({
        "source_file_id": source_file_id,
        "source_file_type": source_file.file_type,
        "modality_slug": modality_slug,
        "edited_at": now.isoformat(),
        "edited_by": request.user.username,
        "edit_meta": edit_meta,
    })

    processed_row = FileRegistry.objects.create(
        file_type=processed_type,
        file_path=object_key,
        file_size=file_size,
        file_hash=hash_sha256.hexdigest(),
        metadata=metadata,
        modality=modality_fk,
        domain="maxillo",
        patient=patient,
    )

    return JsonResponse({
        "success": True,
        "file_id": processed_row.id,
        "url": _serve_file_url(request, processed_row.id),
        "processed_file_type": processed_type,
    })
