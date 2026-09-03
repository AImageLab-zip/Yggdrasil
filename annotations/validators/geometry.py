"""Shape rules: dimensionality, point counts, coordinate ranges.

Pure. Every function here takes plain values and raises ``ValidationError`` or
returns; none of them touch the database, object storage or a model instance.
That is what lets the same rules run in a service before a write, in a
management command over legacy rows, and in a test with a literal dict.

The rules are about *coherence*, not plausibility. Nothing here asks whether a
polygon is a sensible shape for a tooth -- only whether a row claiming to be a
polygon actually holds one, in a frame that can express it. A row that passes
these checks may still be clinically wrong; a row that fails them cannot be
interpreted at all.
"""

import math

from django.core.exceptions import ValidationError

from annotations.constants import CoordinateSystem, Geometry2DType, Geometry3DType

#: Minimum point count per 2D geometry type. ``None`` means "exactly the number
#: in :data:`EXACT_2D_POINTS`".
MIN_2D_POINTS = {
    Geometry2DType.POLYLINE: 2,
    Geometry2DType.POLYGON: 3,
    Geometry2DType.FREEHAND: 2,
}

#: Exact point count per 2D geometry type, where the shape is fully determined.
EXACT_2D_POINTS = {
    Geometry2DType.POINT: 1,
    # Two opposite corners of the axis-aligned bounding box.
    Geometry2DType.RECTANGLE: 2,
    # Likewise: the ellipse inscribed in that box. A rotated ellipse is not
    # representable and is deliberately out of scope -- adding an angle to
    # ``attributes`` later is additive, whereas guessing a convention now and
    # changing it later would silently reinterpret stored rows.
    Geometry2DType.ELLIPSE: 2,
    # Centre, then any point on the circumference. Storing the radius instead
    # would need a unit, and the unit would have to agree with the coordinate
    # system on every read path.
    Geometry2DType.CIRCLE: 2,
}

MIN_3D_POINTS = {
    Geometry3DType.POLYLINE: 2,
}

EXACT_3D_POINTS = {
    Geometry3DType.POINT: 1,
    # Origin, then a point along each of two axes.
    Geometry3DType.PLANE: 3,
    # Two opposite corners.
    Geometry3DType.BOX: 2,
    # Centre only; the radius lives in ``attributes['radius']``.
    Geometry3DType.SPHERE: 1,
}


def _check_ordinates(points, *, expected_length, coordinate_system):
    """Every point is a fixed-length sequence of finite numbers, in range."""
    normalized = coordinate_system in CoordinateSystem.NORMALIZED
    cleaned = []
    for index, point in enumerate(points):
        if isinstance(point, (str, bytes)) or not hasattr(point, "__len__"):
            raise ValidationError(
                f"point {index} must be a sequence of {expected_length} numbers"
            )
        if len(point) != expected_length:
            raise ValidationError(
                f"point {index} has {len(point)} ordinates, expected {expected_length}"
            )
        values = []
        for ordinate in point:
            if isinstance(ordinate, bool) or not isinstance(ordinate, (int, float)):
                raise ValidationError(f"point {index} contains a non-numeric ordinate")
            value = float(ordinate)
            if not math.isfinite(value):
                raise ValidationError(
                    f"point {index} contains a non-finite ordinate; NaN and infinity "
                    "are not coordinates"
                )
            if normalized and not (0.0 <= value <= 1.0):
                raise ValidationError(
                    f"point {index} has ordinate {value} outside [0, 1]; "
                    f"{coordinate_system} is a fraction of the extent, not a pixel index"
                )
            values.append(value)
        cleaned.append(values)
    return cleaned


def _check_count(geometry_type, count, *, exact_table, min_table, what):
    exact = exact_table.get(geometry_type)
    if exact is not None:
        if count != exact:
            raise ValidationError(
                f"a {what} {geometry_type} has exactly {exact} point(s), got {count}"
            )
        return
    minimum = min_table.get(geometry_type)
    if minimum is not None and count < minimum:
        raise ValidationError(
            f"a {what} {geometry_type} needs at least {minimum} points, got {count}"
        )


def validate_geometry_2d(*, geometry_type, coordinate_system, points, closed=False):
    """Validate a planar shape, returning its points as floats.

    Returning the cleaned points rather than just raising is deliberate: the
    caller writing the row should store what was validated, not the original
    input, so an integer pixel index and its float twin cannot round-trip
    differently.
    """
    if geometry_type not in Geometry2DType.ALL:
        raise ValidationError(f"unknown 2D geometry type {geometry_type!r}")
    if coordinate_system not in CoordinateSystem.TWO_D:
        raise ValidationError(
            f"{coordinate_system!r} is not a planar coordinate system; a 2D shape "
            f"cannot be expressed in it"
        )
    if not isinstance(points, (list, tuple)):
        raise ValidationError("points must be a list")

    _check_count(
        geometry_type, len(points), exact_table=EXACT_2D_POINTS, min_table=MIN_2D_POINTS, what="2D"
    )
    cleaned = _check_ordinates(
        points, expected_length=2, coordinate_system=coordinate_system
    )

    if geometry_type == Geometry2DType.POLYGON and not closed:
        raise ValidationError(
            "a polygon is closed by definition; an open ring is a polyline"
        )
    return cleaned


def validate_geometry_3d(
    *, geometry_type, coordinate_system, points, frame_of_reference_uid="", attributes=None
):
    """Validate a shape in a three-space frame, returning its points as floats."""
    if geometry_type not in Geometry3DType.ALL:
        raise ValidationError(f"unknown 3D geometry type {geometry_type!r}")
    if coordinate_system not in CoordinateSystem.THREE_D:
        raise ValidationError(
            f"{coordinate_system!r} is not a three-space coordinate system; a 3D "
            f"shape cannot be expressed in it"
        )
    if not isinstance(points, (list, tuple)):
        raise ValidationError("points must be a list")

    _check_count(
        geometry_type, len(points), exact_table=EXACT_3D_POINTS, min_table=MIN_3D_POINTS, what="3D"
    )
    cleaned = _check_ordinates(
        points, expected_length=3, coordinate_system=coordinate_system
    )

    # A Frame of Reference UID asserts that these coordinates are comparable
    # with any other series sharing it. Voxel indices and a mesh's object space
    # are scoped to one resource, so the assertion would be false there -- and a
    # false one is worse than none, because a later fusion would trust it.
    if frame_of_reference_uid and coordinate_system in {
        CoordinateSystem.VOLUME_VOXEL,
        CoordinateSystem.RESOURCE_LOCAL,
    }:
        raise ValidationError(
            f"{coordinate_system} is scoped to one resource and cannot carry a "
            "frame of reference UID"
        )

    if geometry_type == Geometry3DType.SPHERE:
        radius = (attributes or {}).get("radius")
        if not isinstance(radius, (int, float)) or isinstance(radius, bool):
            raise ValidationError("a sphere needs a numeric attributes['radius']")
        if not math.isfinite(float(radius)) or float(radius) <= 0:
            raise ValidationError("a sphere's radius must be finite and positive")

    return cleaned
