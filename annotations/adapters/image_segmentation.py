"""Moving a 2D labelmap between the browser, the database's payloads and an NPZ file.

Pure apart from numpy and the standard library: no models, no storage. The service owns
where bytes go; this module owns what the bytes *are*.

## The three encodings

- **Wire (browser <-> server):** one plane per label, ``base64(gzip(W*H bytes of 0/1))``.
  The browser has ``CompressionStream`` and no zip library, so the server owns the archive.
  At 1080p four organ masks are ~17 KB, which is why nothing cleverer (bit-packing, RLE)
  is used -- see the M0 numbers in the plan.
- **At rest:** an ``.npz`` per frame with ``shape`` (``[height, width]``) and one ``uint8``
  binary plane ``m_<code>`` per label *present*. Planes are binary and independent, so
  labels may overlap, which a single-valued labelmap cannot express.
- **Summary:** per label ``{"area": pixels, "bbox": [x0, y0, x1, y1]}``, kept in the
  revision's manifest so "which frames contain label X" never opens a file.

A plane is keyed by the label's **code**, never by its integer value or display name:
a rename must not move masks.
"""

import base64
import binascii
import io
import re
import zlib

import numpy as np
from django.core.exceptions import ValidationError

from annotations.validators.image_segmentation import validate_dimensions

_NPZ_PREFIX = "m_"
_VARIANT = re.compile(r"^f(\d+)t(\d+)$")


def frame_variant(file_id, time_ms):
    """The payload ``variant`` for one frame of one source: ``f<fileId>t<timeMs>``."""
    return f"f{int(file_id)}t{int(time_ms)}"


def parse_variant(variant):
    """``(file_id, time_ms)`` from a frame variant, or ``None`` for anything else."""
    match = _VARIANT.match(variant or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def decode_plane(encoded, width, height):
    """A ``(height, width)`` ``uint8`` array of 0/1 from its wire encoding.

    Decompression is bounded to the size the dimensions allow, so a few KB that inflate to
    gigabytes is a ``ValidationError`` and not an out-of-memory.
    """
    validate_dimensions(width, height)
    expected = width * height
    try:
        compressed = base64.b64decode(encoded, validate=True)
    except (binascii.Error, TypeError, ValueError):
        raise ValidationError("a plane is not valid base64")
    inflater = zlib.decompressobj(wbits=31)  # 31 = gzip container
    try:
        raw = inflater.decompress(compressed, expected + 1)
    except zlib.error:
        raise ValidationError("a plane is not valid gzip")
    if len(raw) != expected or inflater.unconsumed_tail:
        raise ValidationError(
            f"a plane must hold exactly {width}x{height} = {expected} bytes"
        )
    plane = np.frombuffer(raw, dtype=np.uint8)
    if plane.size and plane.max() > 1:
        raise ValidationError("plane values must be 0 or 1")
    return plane.reshape(height, width)


def encode_plane(plane):
    """The wire encoding of a plane; the inverse of :func:`decode_plane`."""
    compressor = zlib.compressobj(level=6, wbits=31)
    raw = compressor.compress(np.ascontiguousarray(plane, dtype=np.uint8).tobytes())
    raw += compressor.flush()
    return base64.b64encode(raw).decode("ascii")


def plane_stats(plane):
    """``{"area", "bbox"}`` for a non-empty plane, or ``None`` for an empty one.

    ``bbox`` is ``[x0, y0, x1, y1]`` with inclusive pixel bounds.
    """
    ys, xs = np.nonzero(plane)
    if ys.size == 0:
        return None
    return {
        "area": int(ys.size),
        "bbox": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
    }


def planes_to_npz(width, height, planes):
    """The ``.npz`` bytes for one frame. Empty planes are dropped, not stored."""
    arrays = {
        f"{_NPZ_PREFIX}{code}": np.ascontiguousarray(plane, dtype=np.uint8)
        for code, plane in planes.items()
        if plane.any()
    }
    buffer = io.BytesIO()
    np.savez_compressed(buffer, shape=np.array([height, width], dtype=np.int32), **arrays)
    return buffer.getvalue()


def npz_to_planes(content):
    """``(width, height, {code: plane})`` from :func:`planes_to_npz` bytes."""
    with np.load(io.BytesIO(content), allow_pickle=False) as archive:
        height, width = (int(value) for value in archive["shape"])
        planes = {
            name[len(_NPZ_PREFIX):]: archive[name]
            for name in archive.files
            if name.startswith(_NPZ_PREFIX)
        }
    return width, height, planes
