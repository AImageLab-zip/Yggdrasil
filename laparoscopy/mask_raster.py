"""Turning strokes into the mask layers the NPZ export has always written.

Extracted from ``LaparoscopyExportProcessor`` in Phase 10, unchanged, because two
callers now need it and two implementations of "rasterise this stroke" is two chances
to disagree about a pixel.

That is the whole reason risk 18 -- "decision #15 must preserve NPZ bytes" -- can be
answered by construction rather than by a comparison test. The migration command that
converts the legacy stroke corpus into stored labelmaps calls exactly this code, and the
export that writes the NPZ calls exactly this code, so a mask produced through the new
path and a mask produced through the old one are the same array because they came out of
the same function.

Nothing here knows about the database: strokes arrive as :class:`Stroke`, which is the
three fields the drawing actually reads. ``RegionAnnotation`` rows quack like one and
so do the items rebuilt out of ``annotations/``.
"""

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw


@dataclass(frozen=True)
class Stroke:
    """One drawn mark: a flat ``[x1, y1, x2, y2, ...]`` array and how to paint it.

    ``tool`` is ``brush``, ``eraser`` or ``polygon``. The eraser paints zero into its
    *own* layer only, which is what makes regions independently editable -- erasing
    region A over region B must not remove B, and the layered mask is what allows that.
    """

    points: list
    tool: str
    stroke_width: float = 1.0


def frame_index_for_time(frame_time, fps, frame_count):
    """Seconds to a frame index, clamped into the video.

    Kept identical to the pre-Phase-10 implementation, including its tolerance of
    nonsense input: a stored ``frame_time`` that is NaN becomes frame 0 rather than an
    exception, because an export that dies on one bad row exports nothing at all.
    """
    try:
        frame_time = float(frame_time)
    except (TypeError, ValueError):
        frame_time = 0.0
    if not math.isfinite(frame_time) or frame_time < 0:
        frame_time = 0.0
    frame_index = int(round(frame_time * fps))
    return min(max(frame_index, 0), max(frame_count - 1, 0))


def frame_index_for_ms(time_ms, fps, frame_count):
    """Milliseconds to a frame index, through the same arithmetic.

    The annotation model is integer milliseconds throughout, and stored labelmaps are
    keyed by milliseconds rather than by frame index for the reason the legacy adapter
    already gives: the frame rate is a property of the video file, so a record keyed by
    frame number is only readable next to the decoder that produced it. Re-encoding a
    video at a different rate would silently move every mask.
    """
    return frame_index_for_time((time_ms or 0) / 1000.0, fps, frame_count)


def clamp_coord(value, upper_bound):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value):
        value = 0.0
    if upper_bound <= 0:
        return 0
    return min(max(int(round(value)), 0), upper_bound - 1)


def stroke_pairs(stroke, width, height):
    points = stroke.points if isinstance(stroke.points, list) else []
    if len(points) < 4 or len(points) % 2 != 0:
        return []
    return [
        (clamp_coord(points[index], width), clamp_coord(points[index + 1], height))
        for index in range(0, len(points), 2)
    ]


def _draw_polyline(draw, pairs, fill_value, stroke_width):
    if len(pairs) < 2:
        return
    draw.line(pairs, fill=fill_value, width=stroke_width)
    radius = max(1, stroke_width // 2)
    for x, y in pairs:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill_value)


def apply_stroke_to_layer(image, stroke, width, height):
    """Paint one stroke into one region's 8-bit layer, in place."""
    pairs = stroke_pairs(stroke, width, height)
    if not pairs:
        return

    draw = ImageDraw.Draw(image)
    tool = str(stroke.tool or "").strip().lower()
    stroke_width = max(1, int(round(float(stroke.stroke_width or 1.0))))

    if tool == "polygon":
        if len(pairs) >= 3:
            draw.polygon(pairs, fill=255, outline=255)
        return

    fill_value = 0 if tool == "eraser" else 255
    _draw_polyline(draw, pairs, fill_value, stroke_width)


def render_layers(width, height, class_count, layer_strokes):
    """``(class_count, height, width)`` of 0/1 -- the array the NPZ export writes.

    :param layer_strokes: ``[(class_index, Stroke), ...]`` in the order they were drawn.
        Order matters: the eraser is destructive within its layer, so replaying out of
        order paints back what a later stroke removed.
    """
    if class_count <= 0:
        return np.zeros((0, height, width), dtype=np.uint8)

    layer_images = [Image.new("L", (width, height), 0) for _ in range(class_count)]
    for class_index, stroke in layer_strokes:
        if class_index < 0 or class_index >= class_count:
            continue
        apply_stroke_to_layer(layer_images[class_index], stroke, width, height)
    return np.stack(
        [(np.asarray(image, dtype=np.uint8) > 0).astype(np.uint8) for image in layer_images],
        axis=0,
    )
