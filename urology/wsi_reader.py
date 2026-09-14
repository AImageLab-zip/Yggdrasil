"""Whole Slide Image (WSI) multi-resolution pyramidal TIFF reader.

Extracts IFD metadata, levels, tile dimensions, MPP calibration, and individual
pyramidal tiles on demand using Pillow.
"""

import io
import logging
import re
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple
from PIL import Image, ImageOps, TiffImagePlugin

# Disable PIL decompression bomb limit for high-resolution pathology slides (e.g. >89M pixels)
Image.MAX_IMAGE_PIXELS = None

# Register missing BigTIFF pixel modes in Pillow's TiffImagePlugin table
for byte_order in (b"II", b"MM"):
    for sample_fmt in ((1,), 1):
        for planar in (1, 2):
            TiffImagePlugin.OPEN_INFO[(byte_order, 1, sample_fmt, planar, (8, 8, 8), (2, 2))] = ("RGB", "RGB")
            TiffImagePlugin.OPEN_INFO[(byte_order, 2, sample_fmt, planar, (8, 8, 8), (2, 2))] = ("RGB", "RGB")
            TiffImagePlugin.OPEN_INFO[(byte_order, 6, sample_fmt, planar, (8, 8, 8), (2, 2))] = ("RGB", "RGB")

logger = logging.getLogger(__name__)

# Cache parsed metadata in-process to avoid re-scanning IFD tables on every tile
_METADATA_CACHE: Dict[str, Dict[str, Any]] = {}

# In-memory frame cache for instantaneous tile slicing (up to 100M pixels per frame, e.g. ~300MB uncompressed)
MAX_CACHED_FRAME_PIXELS = 100_000_000
_MAX_CACHED_FRAMES = 8
_FRAME_CACHE: OrderedDict[Tuple[str, int], Image.Image] = OrderedDict()
_FRAME_CACHE_LOCK = threading.Lock()


def _extract_mpp(img: Image.Image) -> Optional[float]:
    """Extract microns-per-pixel (MPP) from TIFF tags or description strings."""
    try:
        # Check TIFF description (common in Aperio SVS: "MPP = 0.2520")
        desc = str(img.tag_v2.get(270, "") or "")
        match = re.search(r"MPP\s*=\s*([0-9.]+)", desc, re.IGNORECASE)
        if match:
            return float(match.group(1))

        # Check XResolution (tag 282) and ResolutionUnit (tag 296)
        x_res = img.tag_v2.get(282)
        unit = img.tag_v2.get(296, 2)  # 2: inch, 3: cm
        if x_res:
            val = float(x_res[0]) / float(x_res[1]) if isinstance(x_res, tuple) else float(x_res)
            if val > 0:
                if unit == 3:  # pixels per cm -> microns per pixel
                    # 1 cm = 10,000 microns
                    return round(10000.0 / val, 4)
                elif unit == 2:  # pixels per inch -> microns per pixel
                    # 1 inch = 25,400 microns
                    return round(25400.0 / val, 4)
    except Exception as exc:
        logger.debug("Could not parse MPP from TIFF tags: %s", exc)
    return None


def get_wsi_metadata(file_source: Any, cache_key: Optional[str] = None) -> Dict[str, Any]:
    """Read pyramidal IFD metadata for a WSI slide."""
    if cache_key and cache_key in _METADATA_CACHE:
        return _METADATA_CACHE[cache_key]

    with Image.open(file_source) as img:
        base_w, base_h = img.size
        mpp = _extract_mpp(img)
        levels = []
        frame = 0

        while True:
            try:
                img.seek(frame)
                w, h = img.size

                # Detect tile size if explicitly tiled, otherwise default to 256
                tile_size = 256
                if hasattr(img, "tile") and img.tile:
                    try:
                        _, tile_box, _, _ = img.tile[0]
                        box_w = tile_box[2] - tile_box[0]
                        if box_w in (256, 512, 1024):
                            tile_size = box_w
                    except Exception:
                        pass

                downsample = round(base_w / w, 4) if w > 0 else 1.0
                cols = (w + tile_size - 1) // tile_size
                rows = (h + tile_size - 1) // tile_size

                levels.append({
                    "level": frame,
                    "width": w,
                    "height": h,
                    "downsample": downsample,
                    "tileSize": tile_size,
                    "tile_size": tile_size,
                    "cols": cols,
                    "rows": rows,
                })
                frame += 1
            except EOFError:
                break

        # If file only had a single IFD (not pre-pyramid), generate virtual downsample levels
        if len(levels) <= 1:
            tile_size = 256
            levels = []
            cur_w, cur_h = base_w, base_h
            lvl = 0
            while cur_w >= tile_size or cur_h >= tile_size or lvl == 0:
                downsample = round(base_w / cur_w, 4) if cur_w > 0 else 1.0
                cols = (cur_w + tile_size - 1) // tile_size
                rows = (cur_h + tile_size - 1) // tile_size
                levels.append({
                    "level": lvl,
                    "width": cur_w,
                    "height": cur_h,
                    "downsample": downsample,
                    "tileSize": tile_size,
                    "tile_size": tile_size,
                    "cols": cols,
                    "rows": rows,
                })
                cur_w = max(1, cur_w // 2)
                cur_h = max(1, cur_h // 2)
                lvl += 1

        chosen_tile_size = levels[0]["tileSize"] if levels else 256
        result = {
            "width": base_w,
            "height": base_h,
            "levels": levels,
            "tileSize": chosen_tile_size,
            "tile_size": chosen_tile_size,
            "mpp": mpp,
            "mpp_x": mpp,
            "mpp_y": mpp,
            "spacing_mm": [mpp / 1000.0, mpp / 1000.0] if mpp else None,
        }

        if cache_key:
            _METADATA_CACHE[cache_key] = result
        return result


def get_wsi_tile(
    file_source: Any,
    level: int,
    col: int,
    row: int,
    tile_size: int = 256,
    image_format: str = "JPEG",
    slide_key: Optional[str] = None,
) -> bytes:
    """Extract and encode a single tile (col, row) at the given pyramid level."""
    # 1. Fast path: check in-memory loaded frame cache for sub-millisecond slicing
    if slide_key:
        with _FRAME_CACHE_LOCK:
            cached_frame = _FRAME_CACHE.get((slide_key, level))
            if cached_frame is not None:
                _FRAME_CACHE.move_to_end((slide_key, level))
                w, h = cached_frame.size
                x0 = col * tile_size
                y0 = row * tile_size
                x1 = min(x0 + tile_size, w)
                y1 = min(y0 + tile_size, h)
                if x0 >= w or y0 >= h or x1 <= x0 or y1 <= y0:
                    tile = Image.new("RGB", (1, 1), (255, 255, 255))
                else:
                    tile = cached_frame.crop((x0, y0, x1, y1))

                buffer = io.BytesIO()
                if image_format.upper() in ("JPEG", "JPG"):
                    tile.save(buffer, format="JPEG", quality=80, subsampling="4:2:0")
                else:
                    tile.save(buffer, format=image_format)
                return buffer.getvalue()

    # 2. Open slide and populate cache if frame size is within memory budget
    with Image.open(file_source) as img:
        has_pyramid = getattr(img, "n_frames", 1) > 1

        if has_pyramid and level < img.n_frames:
            img.seek(level)
            w, h = img.size
            x0 = col * tile_size
            y0 = row * tile_size
            x1 = min(x0 + tile_size, w)
            y1 = min(y0 + tile_size, h)

            if slide_key and (w * h <= MAX_CACHED_FRAME_PIXELS):
                frame_rgb = img.convert("RGB")
                with _FRAME_CACHE_LOCK:
                    _FRAME_CACHE[(slide_key, level)] = frame_rgb
                    _FRAME_CACHE.move_to_end((slide_key, level))
                    if len(_FRAME_CACHE) > _MAX_CACHED_FRAMES:
                        _FRAME_CACHE.popitem(last=False)
                if x0 >= w or y0 >= h or x1 <= x0 or y1 <= y0:
                    tile = Image.new("RGB", (1, 1), (255, 255, 255))
                else:
                    tile = frame_rgb.crop((x0, y0, x1, y1))
            else:
                if x0 >= w or y0 >= h or x1 <= x0 or y1 <= y0:
                    tile = Image.new("RGB", (1, 1), (255, 255, 255))
                else:
                    tile = img.crop((x0, y0, x1, y1)).convert("RGB")

        elif level > 0:
            # Virtual level downsampled from Level 0: check if level 0 is cached
            factor = 2 ** level
            base_frame = None
            if slide_key:
                with _FRAME_CACHE_LOCK:
                    base_frame = _FRAME_CACHE.get((slide_key, 0))

            if base_frame is not None:
                base_w, base_h = base_frame.size
                sx0 = col * tile_size * factor
                sy0 = row * tile_size * factor
                sx1 = min(base_w, (col + 1) * tile_size * factor)
                sy1 = min(base_h, (row + 1) * tile_size * factor)
                if sx0 >= base_w or sy0 >= base_h or sx1 <= sx0 or sy1 <= sy0:
                    tile = Image.new("RGB", (1, 1), (255, 255, 255))
                else:
                    crop = base_frame.crop((sx0, sy0, sx1, sy1))
                    tw = max(1, (sx1 - sx0) // factor)
                    th = max(1, (sy1 - sy0) // factor)
                    tile = crop.resize((tw, th), Image.Resampling.BILINEAR)
            else:
                img.seek(0)
                base_w, base_h = img.size
                sx0 = col * tile_size * factor
                sy0 = row * tile_size * factor
                sx1 = min(base_w, (col + 1) * tile_size * factor)
                sy1 = min(base_h, (row + 1) * tile_size * factor)

                if sx0 >= base_w or sy0 >= base_h or sx1 <= sx0 or sy1 <= sy0:
                    tile = Image.new("RGB", (1, 1), (255, 255, 255))
                else:
                    crop = img.crop((sx0, sy0, sx1, sy1))
                    tw = max(1, (sx1 - sx0) // factor)
                    th = max(1, (sy1 - sy0) // factor)
                    tile = crop.resize((tw, th), Image.Resampling.BILINEAR).convert("RGB")
        else:
            img.seek(0)
            w, h = img.size
            x0 = col * tile_size
            y0 = row * tile_size
            x1 = min(x0 + tile_size, w)
            y1 = min(y0 + tile_size, h)
            if slide_key and (w * h <= MAX_CACHED_FRAME_PIXELS):
                frame_rgb = img.convert("RGB")
                with _FRAME_CACHE_LOCK:
                    _FRAME_CACHE[(slide_key, level)] = frame_rgb
                    _FRAME_CACHE.move_to_end((slide_key, level))
                    if len(_FRAME_CACHE) > _MAX_CACHED_FRAMES:
                        _FRAME_CACHE.popitem(last=False)
                if x0 >= w or y0 >= h or x1 <= x0 or y1 <= y0:
                    tile = Image.new("RGB", (1, 1), (255, 255, 255))
                else:
                    tile = frame_rgb.crop((x0, y0, x1, y1))
            else:
                if x0 >= w or y0 >= h or x1 <= x0 or y1 <= y0:
                    tile = Image.new("RGB", (1, 1), (255, 255, 255))
                else:
                    tile = img.crop((x0, y0, x1, y1)).convert("RGB")

        buffer = io.BytesIO()
        if image_format.upper() in ("JPEG", "JPG"):
            tile.save(buffer, format="JPEG", quality=80, subsampling="4:2:0")
        else:
            tile.save(buffer, format=image_format)
        return buffer.getvalue()


def get_wsi_thumbnail(file_source: Any, max_dim: int = 512, image_format: str = "JPEG") -> bytes:
    """Extract or generate a slide overview thumbnail for the minimap navigator."""
    with Image.open(file_source) as img:
        if getattr(img, "n_frames", 1) > 1:
            # Seek to lowest resolution frame
            img.seek(img.n_frames - 1)
        thumb = img.convert("RGB")
        thumb.thumbnail((max_dim, max_dim), Image.Resampling.BILINEAR)

        buffer = io.BytesIO()
        if image_format.upper() in ("JPEG", "JPG"):
            thumb.save(buffer, format="JPEG", quality=80, subsampling="4:2:0")
        else:
            thumb.save(buffer, format=image_format)
        return buffer.getvalue()
