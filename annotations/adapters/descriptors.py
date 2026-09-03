"""The intermediate vocabulary adapters speak.

An adapter reads a legacy row or a viewer's serialized state and returns a list
of plain dicts describing what to write. It does not write anything, does not
resolve a label to a row, and does not know whether the target already exists --
those need the database, and an adapter that could query would be an adapter
nobody could test with a literal input.

Each descriptor carries an ``"item"`` discriminator naming which service
function applies it. ``annotations.services.apply`` is the dispatcher; adding a
descriptor kind means adding a branch there, which is deliberate -- a silently
ignored descriptor would look like a successful conversion that dropped data.

Labels are referenced by ``label_code`` (the external identifier, e.g. an FDI
tooth number), never by ``LabelDefinition`` id or by integer value. A legacy row
knows its FDI code; it has no idea what integer this installation's schema
assigned to it, and hard-coding one would break the moment a schema is
re-versioned.
"""

GEOMETRY_2D = "geometry_2d"
SPATIAL_3D = "spatial_3d"
MEASUREMENT = "measurement"
TEMPORAL = "temporal"
EVENT = "event"

#: Every discriminator the dispatcher understands. A descriptor whose ``item``
#: is absent from this set is an error, not a no-op.
ITEM_KINDS = frozenset({GEOMETRY_2D, SPATIAL_3D, MEASUREMENT, TEMPORAL, EVENT})


def _base(item, *, label_code=None, selector=None, order=0, attributes=None, role=None):
    descriptor = {"item": item, "order": order, "attributes": attributes or {}}
    if label_code is not None:
        descriptor["label_code"] = label_code
    if selector is not None:
        descriptor["selector"] = selector
    if role is not None:
        descriptor["role"] = role
    return descriptor


def geometry_2d(
    *,
    geometry_type,
    coordinate_system,
    points,
    closed=False,
    stroke_width=None,
    **kwargs,
):
    """A planar shape to write."""
    descriptor = _base(GEOMETRY_2D, **kwargs)
    descriptor.update(
        geometry_type=geometry_type,
        coordinate_system=coordinate_system,
        points=points,
        closed=closed,
        stroke_width=stroke_width,
    )
    return descriptor


def spatial_3d(
    *, geometry_type, coordinate_system, points, frame_of_reference_uid="", **kwargs
):
    """A shape in a three-space frame."""
    descriptor = _base(SPATIAL_3D, **kwargs)
    descriptor.update(
        geometry_type=geometry_type,
        coordinate_system=coordinate_system,
        points=points,
        frame_of_reference_uid=frame_of_reference_uid,
    )
    return descriptor


def measurement(*, kind, value, unit, is_calibrated=False, calibration_note="", sample_count=None, **kwargs):
    """A number, with the unit it has earned."""
    descriptor = _base(MEASUREMENT, **kwargs)
    descriptor.update(
        kind=kind,
        value=value,
        unit=unit,
        is_calibrated=is_calibrated,
        calibration_note=calibration_note,
        sample_count=sample_count,
    )
    return descriptor


def temporal(*, start_time_ms, end_time_ms, **kwargs):
    """A half-open span of a video, in integer milliseconds."""
    descriptor = _base(TEMPORAL, **kwargs)
    descriptor.update(start_time_ms=start_time_ms, end_time_ms=end_time_ms)
    return descriptor


def event(*, event_type, value="", time_ms=None, **kwargs):
    """A categorical statement."""
    descriptor = _base(EVENT, **kwargs)
    descriptor.update(event_type=event_type, value=value, time_ms=time_ms)
    return descriptor


def slice_selector(*, axis, index, coordinate_system):
    """A selector descriptor naming one slice of a volume."""
    return {
        "kind": "slice",
        "coordinate_system": coordinate_system,
        "slice_axis": axis,
        "slice_index": index,
    }


def frame_selector(*, index, coordinate_system):
    """A selector descriptor naming one frame of a video."""
    return {
        "kind": "frame",
        "coordinate_system": coordinate_system,
        "frame_index": index,
    }


def interval_selector(*, start_time_ms, end_time_ms, coordinate_system):
    """A selector descriptor naming a span of a video."""
    return {
        "kind": "temporal_interval",
        "coordinate_system": coordinate_system,
        "start_time_ms": start_time_ms,
        "end_time_ms": end_time_ms,
    }
