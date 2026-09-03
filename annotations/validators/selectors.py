"""Selector rules: a selector must carry exactly the fields its kind needs.

The failure this prevents is a selector that *looks* addressed and is not -- a
``slice`` row with an index and no axis, a ``frame`` row with a timestamp
instead of a frame number. Those read back as valid objects and resolve to the
wrong content, or to nothing, at the point where somebody is looking at a
patient's scan.

So the rules are two-sided: each kind's own fields are required, and every field
belonging to another kind must be absent. A ``frame`` selector that also carries
``slice_index`` is rejected rather than quietly ignored, because "quietly
ignored" is how the wrong one ends up being the one that gets read.

Pure: values in, ``ValidationError`` out.
"""

from django.core.exceptions import ValidationError

from annotations.constants import CoordinateSystem, SelectorKind, SliceAxis

#: Field names each selector kind requires, keyed by kind.
REQUIRED_FIELDS = {
    SelectorKind.WHOLE_RESOURCE: frozenset(),
    SelectorKind.FRAME: frozenset({"frame_index"}),
    SelectorKind.SLICE: frozenset({"slice_axis", "slice_index"}),
    SelectorKind.TEMPORAL_INTERVAL: frozenset({"start_time_ms", "end_time_ms"}),
    SelectorKind.SPATIAL_BOUNDS: frozenset({"bounds"}),
    SelectorKind.SEGMENT: frozenset({"segment_value"}),
}

#: Every addressing field a selector may hold. Anything not required by the
#: kind must be empty.
ADDRESSING_FIELDS = frozenset(
    {
        "frame_index",
        "slice_axis",
        "slice_index",
        "start_time_ms",
        "end_time_ms",
        "segment_value",
        "bounds",
    }
)


def _is_empty(name, value):
    if name == "slice_axis":
        return not value
    if name == "bounds":
        return not value
    return value is None


def _require_non_negative_int(name, value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer, got {value!r}")
    if value < 0:
        raise ValidationError(f"{name} must not be negative, got {value}")


def validate_bounds(bounds, coordinate_system):
    """A spatial-bounds extent: ``{'min': [...], 'max': [...]}``, min <= max.

    The ordinate count is checked against the coordinate system rather than
    fixed at two or three, so a bounds box in ``patient_lps_mm`` and one in
    ``image_pixel`` are both expressible and neither can be written with the
    wrong arity.
    """
    if not isinstance(bounds, dict):
        raise ValidationError("bounds must be an object with 'min' and 'max'")
    missing = {"min", "max"} - set(bounds)
    if missing:
        raise ValidationError(f"bounds is missing {sorted(missing)}")

    expected = 3 if coordinate_system in CoordinateSystem.THREE_D else 2
    lower, upper = bounds["min"], bounds["max"]
    for name, corner in (("min", lower), ("max", upper)):
        if not isinstance(corner, (list, tuple)) or len(corner) != expected:
            raise ValidationError(
                f"bounds['{name}'] must hold {expected} ordinates for {coordinate_system}"
            )
        for ordinate in corner:
            if isinstance(ordinate, bool) or not isinstance(ordinate, (int, float)):
                raise ValidationError(f"bounds['{name}'] contains a non-numeric ordinate")
    for axis, (low, high) in enumerate(zip(lower, upper)):
        if float(high) < float(low):
            raise ValidationError(
                f"bounds axis {axis} has max {high} below min {low}"
            )


def validate_selector(
    *,
    kind,
    coordinate_system,
    frame_index=None,
    slice_axis="",
    slice_index=None,
    start_time_ms=None,
    end_time_ms=None,
    segment_value=None,
    bounds=None,
):
    """Validate one selector's field set against its kind."""
    if kind not in SelectorKind.ALL:
        raise ValidationError(f"unknown selector kind {kind!r}")
    if coordinate_system not in CoordinateSystem.ALL:
        raise ValidationError(f"unknown coordinate system {coordinate_system!r}")

    values = {
        "frame_index": frame_index,
        "slice_axis": slice_axis,
        "slice_index": slice_index,
        "start_time_ms": start_time_ms,
        "end_time_ms": end_time_ms,
        "segment_value": segment_value,
        "bounds": bounds,
    }
    required = REQUIRED_FIELDS[kind]

    for name in sorted(required):
        if _is_empty(name, values[name]):
            raise ValidationError(f"a {kind} selector requires {name}")
    for name in sorted(ADDRESSING_FIELDS - required):
        if not _is_empty(name, values[name]):
            raise ValidationError(
                f"a {kind} selector must not carry {name}; it addresses nothing here "
                "and would be read as if it did"
            )

    if kind == SelectorKind.FRAME:
        _require_non_negative_int("frame_index", frame_index)
    elif kind == SelectorKind.SLICE:
        _require_non_negative_int("slice_index", slice_index)
        if slice_axis not in SliceAxis.ALL:
            raise ValidationError(f"unknown slice axis {slice_axis!r}")
    elif kind == SelectorKind.TEMPORAL_INTERVAL:
        _require_non_negative_int("start_time_ms", start_time_ms)
        _require_non_negative_int("end_time_ms", end_time_ms)
        if end_time_ms < start_time_ms:
            raise ValidationError(
                f"the interval ends at {end_time_ms}ms, before it starts at {start_time_ms}ms"
            )
    elif kind == SelectorKind.SEGMENT:
        _require_non_negative_int("segment_value", segment_value)
    elif kind == SelectorKind.SPATIAL_BOUNDS:
        validate_bounds(bounds, coordinate_system)


def validate_item_selector_pairing(*, coordinate_system, selector_kind, selector_axis=""):
    """Some coordinate systems are only interpretable through a specific selector.

    ``slice_pixel`` is the case that matters today: a pair of numbers inside "the
    axial slice" means nothing without knowing *which* axial slice, and the slice
    lives on the selector, not the geometry. The panoramic arch is stored exactly
    this way -- a spline in the plane of one axial index -- so a spline row whose
    selector has been dropped or replaced with a frame selector is a spline
    nobody can place. Rejecting the combination is how that stays impossible.

    ``selector_kind`` is ``None`` when the item has no selector at all.
    """
    if coordinate_system == CoordinateSystem.SLICE_PIXEL:
        if selector_kind != SelectorKind.SLICE:
            raise ValidationError(
                "slice_pixel coordinates need a slice selector; without the axis "
                "and index the pair cannot be placed in the volume"
            )
        if selector_axis not in SliceAxis.ALL:
            raise ValidationError("a slice selector needs a known axis")
    if (
        coordinate_system in {CoordinateSystem.VIDEO_PIXEL, CoordinateSystem.VIDEO_NORMALIZED}
        and selector_kind is not None
        and selector_kind not in {SelectorKind.FRAME, SelectorKind.TEMPORAL_INTERVAL}
    ):
        raise ValidationError(
            f"video coordinates need a frame or temporal selector, not {selector_kind}"
        )
