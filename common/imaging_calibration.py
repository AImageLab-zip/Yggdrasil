"""Millimetres per pixel for a 2D image, and where that number is allowed to come from.

A photograph or a teleradiography scan has no intrinsic scale. Cornerstone reports
lengths on one in ``px`` and labels them uncalibrated, which is the honest answer and is
what the viewer does until somebody measures a known distance on the image.

That measurement is a property of the *image*, not of annotation work, which is why it
lives in ``FileRegistry.metadata['pixel_spacing_mm']`` and not in an ``AnnotationSet``.
Filing it as annotation work would mean deleting the measurements deleted the
calibration, and the next person to open the study would get pixels again with nothing
saying why.

No migration: ``FileRegistry.metadata`` is a ``JSONField`` already carrying per-file data
for this exact family of surfaces -- ``maxillo.views.intraoral_segmentation`` writes
``image_width``/``image_height`` into it the first time it needs to bound-check a polygon.
"""

import math

#: Where the number lives on a ``FileRegistry`` row.
METADATA_KEY = "pixel_spacing_mm"

#: The previous values, newest last. Kept so a length that looks wrong months later can
#: be explained by a recalibration rather than by a mystery.
HISTORY_KEY = "pixel_spacing_mm_history"


class CalibrationError(ValueError):
    """The two points and the length do not describe a scale anybody can use."""


def spacing_from_known_length(point_a, point_b, known_length_mm):
    """Millimetres per pixel, from two image points and the real distance between them.

    Two points give **one** scalar. A single drawn line cannot distinguish horizontal
    from vertical scale, so this returns an isotropic spacing and says so, rather than
    writing the same number into ``x_mm`` and ``y_mm`` as though each had been measured.
    An anisotropic calibration would need two lines and is not what the UI asks for.

    Recomputed here rather than accepted from the client for the same reason
    ``annotations.adapters.cornerstone`` recomputes every measurement instead of reading
    ``cachedStats``: a number the server cannot re-derive is a number nobody can check,
    and this one silently rescales every length ever taken on the image.

    :returns: ``(mm_per_pixel, pixel_distance)``.
    :raises CalibrationError: on a non-finite input, a non-positive length, or two
        points close enough that the division is meaningless.
    """
    coordinates = []
    for name, point in (("pointA", point_a), ("pointB", point_b)):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise CalibrationError(f"{name} must be two image coordinates")
        for ordinate in point:
            if isinstance(ordinate, bool) or not isinstance(ordinate, (int, float)):
                raise CalibrationError(f"{name} coordinates must be numbers")
            if not math.isfinite(ordinate):
                raise CalibrationError(f"{name} coordinates must be finite")
        coordinates.append([float(point[0]), float(point[1])])

    if isinstance(known_length_mm, bool) or not isinstance(known_length_mm, (int, float)):
        raise CalibrationError("knownLengthMm must be a number")
    if not math.isfinite(known_length_mm) or known_length_mm <= 0:
        raise CalibrationError("knownLengthMm must be a positive number of millimetres")

    pixel_distance = math.dist(coordinates[0], coordinates[1])
    # Not merely "not zero": a line a fraction of a pixel long divides a real length by
    # almost nothing, and the resulting scale would be enormous and confident.
    if pixel_distance < 1.0:
        raise CalibrationError(
            "the two points are less than one pixel apart; a scale derived from them "
            "would be dominated by where the clicks landed"
        )

    return known_length_mm / pixel_distance, pixel_distance


def pixel_spacing_mm(file_obj):
    """``(x_mm, y_mm)`` for a file, or ``None`` when it has never been calibrated.

    ``None`` is load-bearing: the metadata provider omits ``pixelSpacing`` entirely for
    it, which is what makes Cornerstone report ``px`` and mark the measurement
    uncalibrated. Returning a default of 1.0 would report a fiction in millimetres.

    A bare number is accepted as an isotropic spacing, so a value written by hand or by
    a future importer does not crash a viewer for want of the full shape.
    """
    metadata = getattr(file_obj, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    stored = metadata.get(METADATA_KEY)

    if isinstance(stored, bool):
        return None
    if isinstance(stored, (int, float)):
        return _positive_pair(stored, stored)
    if isinstance(stored, dict):
        return _positive_pair(stored.get("x_mm"), stored.get("y_mm"))
    return None


def _positive_pair(x_mm, y_mm):
    values = []
    for value in (x_mm, y_mm):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        values.append(float(value))
    return values[0], values[1]


def calibration_record(mm_per_pixel, *, known_length_mm, pixel_distance, user, now):
    """The value written to ``metadata['pixel_spacing_mm']``, with its provenance.

    An object rather than a bare number, because "who calibrated this, from what" is the
    question that decides whether a millimetre reading is defensible. A number on its own
    cannot answer it, and by the time anybody asks, the person who clicked is gone.
    """
    return {
        "x_mm": mm_per_pixel,
        "y_mm": mm_per_pixel,
        "source": "known_length",
        "known_length_mm": float(known_length_mm),
        "pixel_distance": pixel_distance,
        "calibrated_at": now.isoformat(),
        "calibrated_by": getattr(user, "username", "") or "",
    }
