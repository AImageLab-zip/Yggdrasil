"""Urology WSI tile, metadata, and file serving API views."""

import io
import json
import logging
import mimetypes
import os
import tempfile
import threading
from collections import OrderedDict

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods

from common.file_access import authorize_file_read, open_binary, streaming_response
from common.models import FileRegistry

from .wsi_reader import get_wsi_metadata, get_wsi_thumbnail, get_wsi_tile

logger = logging.getLogger(__name__)

# Local slide and tile disk cache to eliminate repeated S3 downloads and TIFF decompression
WSI_CACHE_DIR = os.path.join(tempfile.gettempdir(), "ygg_wsi_cache")
WSI_TILES_DIR = os.path.join(WSI_CACHE_DIR, "tiles")
os.makedirs(WSI_TILES_DIR, exist_ok=True)

# In-memory LRU tile cache for sub-millisecond tile serving
_TILE_CACHE = OrderedDict()
_TILE_CACHE_LOCK = threading.Lock()
_MAX_TILE_CACHE_SIZE = 2048


def _get_cached_tile(key: str):
    with _TILE_CACHE_LOCK:
        if key in _TILE_CACHE:
            _TILE_CACHE.move_to_end(key)
            return _TILE_CACHE[key]
    return None


def _set_cached_tile(key: str, data: bytes):
    with _TILE_CACHE_LOCK:
        _TILE_CACHE[key] = data
        _TILE_CACHE.move_to_end(key)
        if len(_TILE_CACHE) > _MAX_TILE_CACHE_SIZE:
            _TILE_CACHE.popitem(last=False)


def _get_local_slide_path(file_obj) -> str:
    """Retrieve or cache the slide file locally on disk."""
    safe_hash = file_obj.file_hash or f"file_{file_obj.id}"
    local_path = os.path.join(WSI_CACHE_DIR, f"{safe_hash}.tif")
    if not os.path.exists(local_path):
        body, _ = open_binary(file_obj.file_path)
        tmp_path = f"{local_path}.{os.getpid()}.tmp"
        with open(tmp_path, "wb") as f_out:
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                f_out.write(chunk)
        os.replace(tmp_path, local_path)
    return local_path


def prune_wsi_cache(max_bytes: int = 10 * 1024 * 1024 * 1024) -> int:
    """Prune oldest local slide files and tile caches if total size exceeds max_bytes.

    Returns the total number of bytes freed.
    """
    if not os.path.exists(WSI_CACHE_DIR):
        return 0
    try:
        entries = []
        total_size = 0
        for root, _, files in os.walk(WSI_CACHE_DIR):
            for fname in files:
                fpath = os.path.join(root, fname)
                try:
                    stat = os.stat(fpath)
                    entries.append((stat.st_mtime, stat.st_size, fpath))
                    total_size += stat.st_size
                except OSError:
                    continue
        if total_size <= max_bytes:
            return 0

        # Sort oldest first
        entries.sort(key=lambda x: x[0])
        freed = 0
        target_size = int(max_bytes * 0.8)  # Free down to 80% of ceiling
        for _, size, fpath in entries:
            try:
                os.remove(fpath)
                freed += size
                total_size -= size
                if total_size <= target_size:
                    break
            except OSError:
                continue
        return freed
    except Exception as exc:
        logger.warning("Error pruning WSI cache: %s", exc)
        return 0


def _get_authorized_file(request, file_id: int):
    file_obj = FileRegistry.objects.filter(id=file_id, domain="urology").first()
    if not file_obj:
        raise Http404("File not found")

    allowed, error, status = authorize_file_read(request.user, file_obj, "urology")
    if not allowed:
        logger.warning(
            "User %s denied access to urology file %s (%s)",
            request.user.id,
            file_id,
            error,
        )
        return None, JsonResponse({"error": error}, status=status)

    if file_obj.file_type == "urology_segmentation":
        parent_id = file_obj.metadata.get("associated_image_file_id") if file_obj.metadata else None
        if parent_id:
            parent_file = FileRegistry.objects.filter(id=parent_id, domain="urology").first()
            if parent_file:
                return parent_file, None

    return file_obj, None


@login_required
@require_http_methods(["GET"])
def wsi_metadata_api(request, file_id: int):
    """Return JSON metadata describing the WSI pyramidal levels and physical calibration."""
    file_obj, error_resp = _get_authorized_file(request, file_id)
    if error_resp:
        return error_resp

    try:
        slide_path = _get_local_slide_path(file_obj)
        metadata = get_wsi_metadata(slide_path, cache_key=f"urology_wsi_{file_id}_{file_obj.file_hash}")
        metadata["fileId"] = file_obj.id
        metadata["filename"] = file_obj.metadata.get("original_filename", "") or ""
        return JsonResponse(metadata)
    except Exception as exc:
        logger.exception("Error extracting WSI metadata for file %s: %s", file_id, exc)
        return JsonResponse({"error": str(exc)}, status=500)


@login_required
@require_http_methods(["GET"])
def wsi_tile_api(request, file_id: int, level: int, col: int, row: int):
    """Serve a single tile at (level, col, row) with memory and disk caching."""
    file_obj, error_resp = _get_authorized_file(request, file_id)
    if error_resp:
        return error_resp

    is_png = request.path.endswith(".png") or request.GET.get("format") == "png"
    img_format = "PNG" if is_png else "JPEG"
    content_type = "image/png" if is_png else "image/jpeg"

    safe_hash = file_obj.file_hash or f"file_{file_obj.id}"
    etag = f'"{safe_hash}_{level}_{col}_{row}"'
    if request.headers.get("If-None-Match") == etag:
        return HttpResponse(status=304)

    cache_key = f"{safe_hash}_{level}_{col}_{row}_{img_format}"
    cached_bytes = _get_cached_tile(cache_key)
    if cached_bytes:
        response = HttpResponse(cached_bytes, content_type=content_type)
        response["ETag"] = etag
        response["Cache-Control"] = "public, max-age=604800, immutable"
        return response

    ext = "png" if is_png else "jpg"
    slide_tile_dir = os.path.join(WSI_TILES_DIR, safe_hash)
    disk_tile_path = os.path.join(slide_tile_dir, f"{level}_{col}_{row}.{ext}")

    if os.path.exists(disk_tile_path):
        try:
            with open(disk_tile_path, "rb") as tf:
                tile_bytes = tf.read()
            _set_cached_tile(cache_key, tile_bytes)
            response = HttpResponse(tile_bytes, content_type=content_type)
            response["ETag"] = etag
            response["Cache-Control"] = "public, max-age=604800, immutable"
            return response
        except Exception:
            pass

    try:
        slide_path = _get_local_slide_path(file_obj)
        tile_bytes = get_wsi_tile(
            slide_path,
            level=level,
            col=col,
            row=row,
            tile_size=256,
            image_format=img_format,
            slide_key=safe_hash,
        )

        try:
            os.makedirs(slide_tile_dir, exist_ok=True)
            tmp_tile_path = f"{disk_tile_path}.{os.getpid()}.tmp"
            with open(tmp_tile_path, "wb") as f_out:
                f_out.write(tile_bytes)
            os.replace(tmp_tile_path, disk_tile_path)
        except Exception as write_err:
            logger.debug("Failed saving tile to disk cache: %s", write_err)

        _set_cached_tile(cache_key, tile_bytes)

        response = HttpResponse(tile_bytes, content_type=content_type)
        response["ETag"] = etag
        response["Cache-Control"] = "public, max-age=604800, immutable"
        return response
    except Exception as exc:
        logger.exception("Error reading WSI tile (%s, %s, %s) for file %s: %s", level, col, row, file_id, exc)
        return JsonResponse({"error": str(exc)}, status=500)


@login_required
@require_http_methods(["GET"])
def wsi_thumbnail_api(request, file_id: int):
    """Serve slide overview thumbnail for the floating minimap navigator."""
    file_obj, error_resp = _get_authorized_file(request, file_id)
    if error_resp:
        return error_resp

    safe_hash = file_obj.file_hash or f"file_{file_obj.id}"
    etag = f'"{safe_hash}_thumb"'
    if request.headers.get("If-None-Match") == etag:
        return HttpResponse(status=304)

    slide_tile_dir = os.path.join(WSI_TILES_DIR, safe_hash)
    disk_thumb_path = os.path.join(slide_tile_dir, "thumbnail.jpg")
    if os.path.exists(disk_thumb_path):
        try:
            with open(disk_thumb_path, "rb") as tf:
                thumb_bytes = tf.read()
            response = HttpResponse(thumb_bytes, content_type="image/jpeg")
            response["ETag"] = etag
            response["Cache-Control"] = "public, max-age=86400"
            return response
        except Exception:
            pass

    try:
        slide_path = _get_local_slide_path(file_obj)
        thumb_bytes = get_wsi_thumbnail(slide_path, max_dim=512, image_format="JPEG")

        try:
            os.makedirs(slide_tile_dir, exist_ok=True)
            tmp_thumb_path = f"{disk_thumb_path}.{os.getpid()}.tmp"
            with open(tmp_thumb_path, "wb") as f_out:
                f_out.write(thumb_bytes)
            os.replace(tmp_thumb_path, disk_thumb_path)
        except Exception as write_err:
            logger.debug("Failed saving thumbnail to disk cache: %s", write_err)

        response = HttpResponse(thumb_bytes, content_type="image/jpeg")
        response["ETag"] = etag
        response["Cache-Control"] = "public, max-age=86400"
        return response
    except Exception as exc:
        logger.exception("Error generating thumbnail for file %s: %s", file_id, exc)
        return JsonResponse({"error": str(exc)}, status=500)


@login_required
@require_http_methods(["GET"])
def serve_file(request, file_id: int, filename: str = None, bundle_key: str = None):
    """Stream a Urology FileRegistry entry (e.g. NIfTI MRI volume) for Cornerstone3D."""
    del filename
    if bundle_key:
        raise Http404("Urology files do not use bundles")

    file_obj, error_resp = _get_authorized_file(request, file_id)
    if error_resp:
        return error_resp

    content_type = None
    original_name = file_obj.metadata.get("original_filename") or ""
    if original_name.endswith(".nii.gz") or (file_obj.file_path and file_obj.file_path.endswith(".gz")):
        content_type = "application/gzip"
    else:
        content_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"

    return streaming_response(
        path_or_key=file_obj.file_path,
        content_type=content_type,
        filename=original_name or "file.nii.gz",
        as_attachment=False,
    )


@login_required
@require_http_methods(["GET"])
def wsi_segmentation_api(request, file_id: int):
    """Return GeoJSON segmentation data for a slide or its linked segmentation file."""
    file_obj, error_resp = _get_authorized_file(request, file_id)
    if error_resp:
        return error_resp

    seg_obj = None
    if file_obj.file_type == "urology_segmentation":
        seg_obj = file_obj
    else:
        # 1. By associated_image_file_id in metadata
        seg_obj = FileRegistry.objects.filter(
            domain="urology",
            file_type="urology_segmentation",
            metadata__associated_image_file_id=file_obj.id,
        ).first()

        # 2. If not found, match by stem on same patient
        if not seg_obj and file_obj.urology_patient:
            orig_name = file_obj.metadata.get("original_filename", "")
            orig_stem = os.path.splitext(orig_name)[0].lower()
            if orig_stem.startswith("copia di "):
                orig_stem = orig_stem[9:].strip()

            candidates = FileRegistry.objects.filter(
                domain="urology",
                file_type="urology_segmentation",
                urology_patient=file_obj.urology_patient,
            )
            for cand in candidates:
                cand_name = cand.metadata.get("original_filename", "")
                cand_stem = os.path.splitext(cand_name)[0].lower()
                if cand_stem.startswith("copia di "):
                    cand_stem = cand_stem[9:].strip()
                if orig_stem and (orig_stem == cand_stem or cand_stem in orig_stem or orig_stem in cand_stem):
                    seg_obj = cand
                    break

    if not seg_obj:
        return JsonResponse({"hasSegmentation": False, "featureCount": 0, "classes": [], "geojson": None})

    try:
        body, _ = open_binary(seg_obj.file_path)
        content = body.read()
        geo_data = json.loads(content.decode("utf-8"))
        features = geo_data.get("features", []) if isinstance(geo_data, dict) else []
        feature_count = len(features)
        classes = seg_obj.metadata.get("classes", [])
        if not classes:
            seen_classes = {}
            for feat in features:
                cls_info = (feat.get("properties") or {}).get("classification")
                if isinstance(cls_info, dict):
                    cname = cls_info.get("name", "Unknown")
                    color = cls_info.get("colorRGB") or cls_info.get("color")
                    if cname not in seen_classes:
                        seen_classes[cname] = color
            classes = [{"name": name, "color": color} for name, color in seen_classes.items()]

        return JsonResponse({
            "hasSegmentation": True,
            "fileId": seg_obj.id,
            "filename": seg_obj.metadata.get("original_filename", "") if isinstance(seg_obj.metadata, dict) else "",
            "featureCount": feature_count,
            "classes": classes,
            "geojson": geo_data,
        })
    except Exception as exc:
        logger.exception("Error loading segmentation for file %s: %s", file_id, exc)
        return JsonResponse({"hasSegmentation": False, "error": str(exc)}, status=500)

