"""Cornerstone3D tool state in, descriptors out.

The Phase 3 volume grid is the first surface where a measurement can be *made*, and
therefore the first where one can be lost. `docs/cornerstone-roadmap.md` opens with the
problem this closes: "One measurement tool exists, and it is never saved."

Pure translation, like every other adapter here: dicts in, descriptor dicts out, no
query and no save. Three rules come from the governing architectural rule rather than
from convenience, and each one is a refusal:

**Runtime identifiers are dropped, not stored.** ``annotationUID``, ``referencedImageId``
and any ``volumeId`` are session-scoped -- they name objects in one browser tab's cache
and mean nothing tomorrow. ``FrameOfReferenceUID`` is the exception and is kept *in
patient space*: that one is DICOM's, not Cornerstone's, and
``SpatialAnnotation3DItem`` has a column for it. It is dropped again in a
resource-scoped frame, where it would be a false claim -- see
:data:`RESOURCE_SCOPED_FRAMES`.

**Measurements are recomputed from the points, never read from ``cachedStats``.** The
serializer's ``assert_no_viewer_identifiers`` already refuses a document containing
``cachedStats``, and this is the same rule one step earlier. A number the store cannot
re-derive is a number nobody can check: if the viewer's arithmetic is wrong -- or its
`cachedStats` is simply stale, which is its normal state between edits -- the wrong
value becomes the record. Every value written here is a function of the geometry
written beside it, so the two can never disagree.

**Intensity statistics are refused outright.** A probe's Hounsfield reading or an ROI's
mean is not derivable from the geometry; it needs the voxels, which an adapter does not
have. Rather than accept the viewer's number for those, {@func descriptors_for_annotation}
writes the shape and omits the statistic. Decision #11 puts ROI statistics in the first
release; they belong to a server-side pass that reads the volume, not to a client that
can be asked to report anything.

Handle conventions below were read off ``@cornerstonejs/tools@5.8.2`` rather than from
documentation -- the source line is cited for each.
"""

import math

from django.core.exceptions import ValidationError

from annotations.adapters import descriptors as d
from annotations.constants import (
    CoordinateSystem,
    Geometry3DType,
    MeasurementKind,
    MeasurementUnit,
)

#: Cornerstone keys that name something only this browser session knows about. Kept as
#: a set so :func:`strip_runtime_identifiers` is a filter rather than a list of
#: deletions somebody has to remember to extend.
RUNTIME_KEYS = frozenset(
    {
        "annotationUID",
        "referencedImageId",
        "volumeId",
        "segmentationId",
        "cachedStats",
        "targetId",
        "toolGroupId",
        "viewportId",
        "renderingEngineId",
    }
)

#: Tools whose value the store can re-derive from the geometry alone.
GEOMETRIC_TOOLS = frozenset(
    {"Length", "Height", "Angle", "CobbAngle", "Bidirectional", "RectangleROI", "EllipticalROI", "CircleROI"}
)

#: Tools that carry geometry but whose *number* needs the voxels. See the module note.
INTENSITY_TOOLS = frozenset({"Probe"})

#: Frames whose coordinates mean nothing outside the one resource that defines them,
#: and which therefore cannot carry a DICOM Frame of Reference UID.
RESOURCE_SCOPED_FRAMES = frozenset(
    {CoordinateSystem.VOLUME_VOXEL, CoordinateSystem.RESOURCE_LOCAL}
)


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValidationError(
            f"a patient-space handle must be three coordinates, got {value!r}"
        )
    out = []
    for ordinate in value:
        if isinstance(ordinate, bool) or not isinstance(ordinate, (int, float)):
            raise ValidationError(f"handle ordinate must be a number, got {ordinate!r}")
        if not math.isfinite(ordinate):
            raise ValidationError("handle ordinates must be finite")
        out.append(float(ordinate))
    return out


def handle_points(annotation):
    """The tool's handles, validated, in patient LPS millimetres.

    Cornerstone's handles are already in the viewport's world frame, which for a volume
    viewport is DICOM patient space. That is why nothing here converts: the numbers
    arrive in the frame the descriptor declares. What it *does* do is refuse anything
    that is not three finite numbers, because a NaN handle reaches the database as a
    perfectly storable null island.
    """
    data = annotation.get("data") or {}
    handles = data.get("handles") or {}
    points = handles.get("points")
    if not isinstance(points, (list, tuple)) or not points:
        raise ValidationError("a Cornerstone annotation must carry at least one handle")
    return [_point(point) for point in points]


def strip_runtime_identifiers(value):
    """Deep-copy a payload with every session-scoped identifier removed.

    A walk rather than a top-level key test, for the same reason the serializer's
    check is a walk: the realistic way one of these gets in is nested inside a tool's
    own ``data`` blob, not at the top of the object.
    """
    if isinstance(value, dict):
        return {
            key: strip_runtime_identifiers(item)
            for key, item in value.items()
            if key not in RUNTIME_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [strip_runtime_identifiers(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Geometry, recomputed
# ---------------------------------------------------------------------------


def _distance(a, b):
    return math.dist(a, b)


def _subtract(a, b):
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _cross(a, b):
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _norm(a):
    return math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2)


def polyline_length(points):
    """Total length of an open polyline, in the points' own units."""
    return sum(_distance(points[i], points[i + 1]) for i in range(len(points) - 1))


def angle_at_vertex(points):
    """The angle at ``points[1]``, in degrees.

    ``AngleTool.js:411-418`` computes ``angleBetweenLines([p0, p1], [p1, p2])``, so the
    middle handle is the vertex. Getting that wrong gives the supplement, which looks
    like a plausible angle and is wrong by however much the real one is not 90 degrees.
    """
    first = _subtract(points[0], points[1])
    second = _subtract(points[2], points[1])
    denominator = _norm(first) * _norm(second)
    if denominator == 0:
        raise ValidationError("an angle needs three distinct points")
    cosine = max(-1.0, min(1.0, sum(a * b for a, b in zip(first, second)) / denominator))
    return math.degrees(math.acos(cosine))


def angle_between_lines(points):
    """The angle between line ``p0-p1`` and line ``p2-p3``, in degrees.

    The Cobb angle: two independent lines rather than a shared vertex, which is why it
    cannot reuse :func:`angle_at_vertex`. Reported in [0, 90] because the two lines are
    undirected -- reversing either handle pair must not change the answer, and it would
    if the raw arccos were returned.
    """
    first = _subtract(points[1], points[0])
    second = _subtract(points[3], points[2])
    denominator = _norm(first) * _norm(second)
    if denominator == 0:
        raise ValidationError("a Cobb angle needs two lines of non-zero length")
    cosine = abs(sum(a * b for a, b in zip(first, second))) / denominator
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def polygon_area(points):
    """Area of a planar polygon given in three-space.

    The shoelace formula generalised: half the magnitude of the summed cross products
    of consecutive vertex pairs about the first vertex. Correct for any planar polygon
    in any orientation, which a 2D shoelace on projected coordinates would not be --
    an oblique reformat's rectangle is not axis-aligned in patient space.
    """
    if len(points) < 3:
        raise ValidationError("an area needs at least three points")
    total = [0.0, 0.0, 0.0]
    origin = points[0]
    for index in range(1, len(points) - 1):
        cross = _cross(_subtract(points[index], origin), _subtract(points[index + 1], origin))
        total = [total[axis] + cross[axis] for axis in range(3)]
    return _norm(total) / 2


def circle_radius(points):
    """Radius from Cornerstone's two circle handles.

    ``CircleROITool.js:181-189``: ``points[0]`` is the centre and ``points[1]`` a point
    on the circumference.
    """
    return _distance(points[0], points[1])


def ellipse_semi_axes(points):
    """The two semi-axes of Cornerstone's four-handle ellipse.

    ``EllipticalROITool.js:275-312`` moves ``points[0]``/``points[1]`` as one axis pair
    and ``points[2]``/``points[3]`` as the other, so each pair spans a full axis and
    the semi-axis is half the distance between them.
    """
    return _distance(points[0], points[1]) / 2, _distance(points[2], points[3]) / 2


# ---------------------------------------------------------------------------
# Tool -> descriptors
# ---------------------------------------------------------------------------


def _require_points(tool_name, points, expected):
    if len(points) != expected:
        raise ValidationError(
            f"{tool_name} needs exactly {expected} handles, got {len(points)}; "
            "an incomplete annotation must not be stored as though it were finished"
        )


def _geometry(geometry_type, points, *, coordinate_system, frame_of_reference_uid, **kwargs):
    return d.spatial_3d(
        geometry_type=geometry_type,
        coordinate_system=coordinate_system,
        points=points,
        frame_of_reference_uid=frame_of_reference_uid,
        **kwargs,
    )


def _measurement(kind, value, unit, *, calibrated, **kwargs):
    return d.measurement(
        kind=kind,
        value=value,
        unit=unit,
        is_calibrated=calibrated,
        calibration_note=(
            ""
            if calibrated
            else "patient-space geometry with no millimetre scale; reported uncalibrated"
        ),
        **kwargs,
    )


def descriptors_for_annotation(
    annotation,
    *,
    coordinate_system=CoordinateSystem.PATIENT_LPS_MM,
    selector=None,
    label_code=None,
    order=0,
):
    """Translate one Cornerstone tool annotation into descriptors to write.

    Returns the geometry descriptor first and any measurements after it, all sharing
    the same ``order`` group so :mod:`annotations.services.apply` writes them together.

    :param annotation: one entry of Cornerstone's annotation state.
    :param coordinate_system: the frame the handles are in. Defaults to
        ``PATIENT_LPS_MM``, which is what a volume viewport's world coordinates are;
        pass something else only if you have actually converted them.
    :raises ValidationError: on an unknown tool, an incomplete handle set, or a
        non-finite coordinate.
    """
    metadata = annotation.get("metadata") or {}
    tool_name = metadata.get("toolName")
    if not tool_name:
        raise ValidationError("a Cornerstone annotation must name the tool that made it")

    if coordinate_system not in CoordinateSystem.ALL:
        raise ValidationError(f"unknown coordinate system {coordinate_system!r}")

    points = handle_points(annotation)
    # Only a millimetre frame earns millimetre units -- the same rule
    # ``MeasurementItem``'s CHECK constraint enforces in the database.
    calibrated = coordinate_system in CoordinateSystem.MILLIMETRE
    length_unit = MeasurementUnit.MM if calibrated else MeasurementUnit.PX
    area_unit = MeasurementUnit.MM2 if calibrated else MeasurementUnit.PX2

    shared = {
        "selector": selector,
        "label_code": label_code,
        "order": order,
    }
    geometry_kwargs = dict(
        coordinate_system=coordinate_system,
        # A Frame of Reference UID asserts these coordinates are comparable with any
        # other series carrying it. That is true in patient space and false in a
        # resource-scoped frame -- voxel indices and a mesh's object space mean nothing
        # outside the one resource that defines them. Cornerstone attaches the UID to
        # every annotation regardless, so it is dropped here rather than passed on: a
        # false claim is worse than none, because a later fusion would trust it.
        # ``annotations.validators.geometry`` refuses it outright, so this is the
        # adapter agreeing with the validator rather than working around it.
        frame_of_reference_uid=(
            str(metadata.get("FrameOfReferenceUID") or "")
            if coordinate_system not in RESOURCE_SCOPED_FRAMES
            else ""
        ),
        **shared,
    )

    if tool_name in ("Length", "Height"):
        _require_points(tool_name, points, 2)
        return [
            _geometry(Geometry3DType.POLYLINE, points, **geometry_kwargs),
            _measurement(
                MeasurementKind.LENGTH,
                polyline_length(points),
                length_unit,
                calibrated=calibrated,
                **shared,
            ),
        ]

    if tool_name == "Angle":
        _require_points(tool_name, points, 3)
        return [
            _geometry(Geometry3DType.POLYLINE, points, **geometry_kwargs),
            _measurement(
                MeasurementKind.ANGLE,
                angle_at_vertex(points),
                MeasurementUnit.DEG,
                calibrated=True,  # an angle is dimensionless; no scale is needed
                **shared,
            ),
        ]

    if tool_name == "CobbAngle":
        _require_points(tool_name, points, 4)
        return [
            _geometry(Geometry3DType.POLYLINE, points, **geometry_kwargs),
            _measurement(
                MeasurementKind.ANGLE,
                angle_between_lines(points),
                MeasurementUnit.DEG,
                calibrated=True,
                **shared,
            ),
        ]

    if tool_name == "Bidirectional":
        _require_points(tool_name, points, 4)
        # Two independent axes, not one polyline: the long axis is handles 0-1 and the
        # short axis 2-3. Summing them as a polyline would report a meaningless total.
        long_axis = _distance(points[0], points[1])
        short_axis = _distance(points[2], points[3])
        return [
            _geometry(Geometry3DType.POLYLINE, points, **geometry_kwargs),
            _measurement(
                MeasurementKind.LENGTH,
                long_axis,
                length_unit,
                calibrated=calibrated,
                role="long_axis",
                **shared,
            ),
            _measurement(
                MeasurementKind.LENGTH,
                short_axis,
                length_unit,
                calibrated=calibrated,
                role="short_axis",
                **shared,
            ),
        ]

    if tool_name == "RectangleROI":
        _require_points(tool_name, points, 4)
        # Cornerstone's four handles are corners, but not in perimeter order.
        # `RectangleROITool.js:215-239` assigns them as
        # (bottomLeft, bottomRight, topLeft, topRight), so index order 0,1,2,3 traces a
        # bow-tie -- whose shoelace area is zero, not merely wrong. The perimeter walk
        # is 0, 1, 3, 2.
        ordered = [points[0], points[1], points[3], points[2]]
        return [
            # The handles are stored as Cornerstone gave them; only the *measurement*
            # walks them in perimeter order. Reordering the stored geometry would make
            # a round trip back into the viewer put the handles in the wrong corners.
            _geometry(Geometry3DType.POLYLINE, points, **geometry_kwargs),
            _measurement(
                MeasurementKind.AREA,
                polygon_area(ordered),
                area_unit,
                calibrated=calibrated,
                **shared,
            ),
            _measurement(
                MeasurementKind.PERIMETER,
                polyline_length(ordered + [ordered[0]]),
                length_unit,
                calibrated=calibrated,
                **shared,
            ),
        ]

    if tool_name == "CircleROI":
        _require_points(tool_name, points, 2)
        radius = circle_radius(points)
        return [
            _geometry(
                Geometry3DType.SPHERE,
                [points[0]],
                attributes={"radius": radius, "radius_unit": length_unit},
                **geometry_kwargs,
            ),
            _measurement(
                MeasurementKind.DIAMETER,
                radius * 2,
                length_unit,
                calibrated=calibrated,
                **shared,
            ),
            _measurement(
                MeasurementKind.AREA,
                math.pi * radius**2,
                area_unit,
                calibrated=calibrated,
                **shared,
            ),
        ]

    if tool_name == "EllipticalROI":
        _require_points(tool_name, points, 4)
        semi_major, semi_minor = ellipse_semi_axes(points)
        return [
            _geometry(Geometry3DType.POLYLINE, points, **geometry_kwargs),
            _measurement(
                MeasurementKind.AREA,
                math.pi * semi_major * semi_minor,
                area_unit,
                calibrated=calibrated,
                **shared,
            ),
        ]

    if tool_name in INTENSITY_TOOLS:
        # The shape is kept; the number is not. See the module docstring: a Hounsfield
        # reading needs the voxels, and taking the viewer's word for it would put an
        # uncheckable value in the canonical store.
        _require_points(tool_name, points, 1)
        return [_geometry(Geometry3DType.POINT, points, **geometry_kwargs)]

    raise ValidationError(
        f"no descriptor mapping for Cornerstone tool {tool_name!r}. "
        "Add one here rather than storing the annotation untranslated: an unmapped "
        "tool written as a bare payload is data the canonical document cannot read."
    )


def cornerstone_state_payload(annotations):
    """The editable ``cornerstone_state`` payload for a revision.

    Kept so a user can resume editing exactly where they left off. **Never canonical**
    (``PayloadFormat`` says so explicitly), free to go stale relative to the items, and
    stripped of runtime identifiers on the way in -- a resumable scratch copy does not
    need last session's ``annotationUID``, and keeping one would make the payload look
    like it identified something.
    """
    return {"annotations": [strip_runtime_identifiers(entry) for entry in annotations]}
