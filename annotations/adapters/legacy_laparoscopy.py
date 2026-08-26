"""Translating laparoscopy's two legacy annotation tables.

Pure: legacy values in, descriptor dicts out.

The interesting conversion here is time. ``RegionAnnotation.frame_time`` is a
float in *seconds*; ``QuadrantClassificationMarker.time_ms`` is an integer in
milliseconds. The annotation model is integer milliseconds throughout, so the
float has to be converted -- and how it is converted is a decision, not an
implementation detail, because the result is a key that has to keep matching the
same video frame forever. :func:`frame_time_to_ms` rounds to the nearest
millisecond and refuses anything that is not a finite, non-negative number,
rather than truncating: at 30 fps a frame is 33.3 ms, so truncation would
systematically bias every marker toward the previous frame.
"""

import math

from django.core.exceptions import ValidationError

from annotations.adapters import descriptors
from annotations.constants import CoordinateSystem, Geometry2DType

#: Legacy tool names on ``RegionAnnotation``, mapped to a geometry type.
#: ``brush`` and ``eraser`` are both freehand strokes -- the eraser's *effect*
#: is destructive (decision #14) but the stroke it recorded is the same shape,
#: and which one it was is kept in ``attributes`` so a replay can tell them
#: apart.
TOOL_GEOMETRY = {
    "brush": Geometry2DType.FREEHAND,
    "eraser": Geometry2DType.FREEHAND,
    "polygon": Geometry2DType.POLYGON,
}


def frame_time_to_ms(frame_time):
    """Seconds as a float -> milliseconds as an integer, rounded to nearest.

    Truncating instead would bias every annotation toward the previous frame:
    at 30 fps a frame boundary lands on 33.3 ms, and ``int(0.0333 * 1000)`` is
    33 while the next is 66 rather than 67. Over a long video that drift is a
    frame, and a frame is the unit the whole surface is addressed by.
    """
    if isinstance(frame_time, bool) or not isinstance(frame_time, (int, float)):
        raise ValidationError(f"frame_time must be a number, got {frame_time!r}")
    value = float(frame_time)
    if not math.isfinite(value):
        raise ValidationError("frame_time must be finite")
    if value < 0:
        raise ValidationError("frame_time must not be negative")
    return int(round(value * 1000))


def _unflatten(points):
    """Konva stores a polyline as a flat ``[x1, y1, x2, y2, ...]`` array."""
    if not isinstance(points, list):
        raise ValidationError("points must be a list")
    if len(points) % 2:
        raise ValidationError(
            f"a flat point array has an even length; got {len(points)} ordinates"
        )
    return [[points[i], points[i + 1]] for i in range(0, len(points), 2)]


def region_annotation(
    *, tool, frame_time, points, stroke_width=None, prompt_points=None, region_name=None
):
    """Convert one ``RegionAnnotation`` row into its descriptors.

    Returns the stroke, and -- where the row carries SAM2 prompt points -- one
    descriptor per prompt point. The two are in *different coordinate systems*:
    the stroke is in video-frame pixels, the prompts are [0, 1] fractions, and
    the API layer already validates the second range. Merging them into one
    frame would corrupt whichever one lost, so the model records each in its
    own.
    """
    geometry_type = TOOL_GEOMETRY.get(tool)
    if geometry_type is None:
        raise ValidationError(f"unknown annotation tool {tool!r}")

    time_ms = frame_time_to_ms(frame_time)
    # A legacy row knows a time, not a frame number: the frame rate is a
    # property of the video file, which an adapter is not allowed to open. So
    # the selector is a zero-length interval, and Phase 10 can resolve frame
    # indices once it has the decoder that knows them.
    selector = descriptors.interval_selector(
        start_time_ms=time_ms,
        end_time_ms=time_ms,
        coordinate_system=CoordinateSystem.VIDEO_PIXEL,
    )

    shared_attributes = {"tool": tool}
    if region_name:
        shared_attributes["region_name"] = region_name

    out = [
        descriptors.geometry_2d(
            geometry_type=geometry_type,
            coordinate_system=CoordinateSystem.VIDEO_PIXEL,
            points=_unflatten(points),
            closed=geometry_type == Geometry2DType.POLYGON,
            stroke_width=stroke_width,
            selector=selector,
            label_code=region_name,
            attributes=shared_attributes,
        )
    ]

    for index, prompt in enumerate(prompt_points or []):
        if not isinstance(prompt, dict) or "x" not in prompt or "y" not in prompt:
            raise ValidationError(f"prompt point {index} must hold x and y")
        out.append(
            descriptors.geometry_2d(
                geometry_type=Geometry2DType.POINT,
                coordinate_system=CoordinateSystem.VIDEO_NORMALIZED,
                points=[[prompt["x"], prompt["y"]]],
                selector=selector,
                label_code=region_name,
                order=index,
                attributes={
                    **shared_attributes,
                    "prompt": True,
                    # 1 is a positive prompt, 0 a negative one; SAM2 needs both
                    # and losing the distinction would invert half of them.
                    "prompt_label": 0 if prompt.get("label") == 0 else 1,
                    "index": index,
                },
            )
        )
    return out


def quadrant_marker(*, time_ms, quadrant_name=None):
    """Convert one ``QuadrantClassificationMarker`` into an event descriptor.

    Already integer milliseconds, so nothing is converted -- which is exactly
    why this table needs no ``annotations_normalize_coordinates`` pass and
    ``RegionAnnotation`` does.
    """
    if isinstance(time_ms, bool) or not isinstance(time_ms, int) or time_ms < 0:
        raise ValidationError("time_ms must be non-negative integer milliseconds")
    return [
        descriptors.event(
            event_type="quadrant",
            value=quadrant_name or "",
            time_ms=time_ms,
            label_code=quadrant_name,
            attributes={"quadrant": quadrant_name} if quadrant_name else {},
        )
    ]
