"""Structural rules for 2D labelmap saves: how many frames, how big, which codes.

Pure: plain values in, ``ValidationError`` out. Whether a label code *exists* in a
project's schema is a database question and is answered by the service; what is checked
here is only what can be said without one.

The limits are corruption guards rather than product decisions. A 1080p plane gzips to
a few KB, so these numbers sit far above any real save and exist to make a runaway client
(or a decompression bomb) a 400 instead of an out-of-memory.
"""

import re

from django.core.exceptions import ValidationError

#: Frames one save may touch. A save names only edited frames, so this is "how much
#: was edited since the last autosave", which is a handful.
MAX_FRAMES_PER_SAVE = 50

#: Labels one frame may carry, which is also how many planes a response can hold.
MAX_LABELS_PER_FRAME = 64

#: Per-side and total pixel caps for one frame. 8192 covers every camera in the system;
#: the pixel cap is 4096x4096, so a wide-but-short frame is allowed and a square 8k one is not.
MAX_DIMENSION = 8192
MAX_PIXELS = 4096 * 4096

#: Upper bound on a frame's timestamp in ms (~115 days), which also keeps the payload
#: ``variant`` (``f<fileId>t<ms>``, at most 40 characters) comfortably inside its column.
MAX_TIME_MS = 10_000_000_000

_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_time_ms(value, *, where="timeMs"):
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{where} must be an integer number of milliseconds")
    if not 0 <= value <= MAX_TIME_MS:
        raise ValidationError(f"{where} must be between 0 and {MAX_TIME_MS}")
    return value


def validate_dimensions(width, height, *, where="frame"):
    for name, value in (("width", width), ("height", height)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"{where}: {name} must be a positive integer")
        if value > MAX_DIMENSION:
            raise ValidationError(f"{where}: {name} {value} exceeds {MAX_DIMENSION}")
    if width * height > MAX_PIXELS:
        raise ValidationError(
            f"{where}: {width}x{height} is more than {MAX_PIXELS} pixels"
        )
    return width, height


def validate_label_code(code, *, where="label"):
    if not isinstance(code, str) or not _CODE.match(code):
        raise ValidationError(f"{where}: {code!r} is not a valid label code")
    return code


def validate_color(value):
    """``#rrggbb``, or blank. Case is normalised so two spellings never differ."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not _COLOR.match(value):
        raise ValidationError("color must be #rrggbb")
    return value.lower()


def validate_frame_batch(frames):
    """The shape of a save's frame list, before any plane is decoded.

    ``frames`` is a list of ``{"time_ms", "width", "height", "planes"}`` where ``planes``
    maps a label code to its still-encoded plane. Returns nothing: it either passes or
    raises, so a caller cannot use a half-checked batch.
    """
    if not isinstance(frames, list) or not frames:
        raise ValidationError("frames must be a non-empty list")
    if len(frames) > MAX_FRAMES_PER_SAVE:
        raise ValidationError(f"a save may touch at most {MAX_FRAMES_PER_SAVE} frames")

    seen = set()
    for index, frame in enumerate(frames):
        where = f"frames[{index}]"
        if not isinstance(frame, dict):
            raise ValidationError(f"{where} must be an object")
        time_ms = validate_time_ms(frame.get("time_ms"), where=f"{where}.timeMs")
        if time_ms in seen:
            raise ValidationError(f"{where} repeats timeMs {time_ms}")
        seen.add(time_ms)
        validate_dimensions(frame.get("width"), frame.get("height"), where=where)
        planes = frame.get("planes")
        if not isinstance(planes, dict):
            raise ValidationError(f"{where}.planes must be an object keyed by label code")
        if len(planes) > MAX_LABELS_PER_FRAME:
            raise ValidationError(
                f"{where} carries {len(planes)} labels; the limit is {MAX_LABELS_PER_FRAME}"
            )
        for code in planes:
            validate_label_code(code, where=f"{where}.planes")
