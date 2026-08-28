"""Re-projecting polygons through the geometric image editor's operations.

``rgb_editor.js`` can crop, mirror and rotate an intraoral photograph, and writes a *new*
``FileRegistry`` row for the result carrying ``metadata['edit_meta']``. Every tooth polygon
already drawn on the original is expressed in the old pixel frame, so reading it back
against the edited image means replaying those operations over the geometry.

Moved here from ``maxillo/views/intraoral_segmentation.py`` when Phase 5 deleted that
module's editor endpoints. This is the right home for it: ``adapters/`` is pure translation
-- values in, values out, no database and no I/O -- and both the annotation read path and
the export now need it, neither of which should reach into a domain app's views.

**Two implementations exist and that is deliberate.** This one is the stored read;
``frontend/imaging/photos/editReplay.js`` is the live preview, which has to run before
anything is saved. They had already drifted once -- this side implemented ``flip-h``,
``flip-v`` and ``crop`` and *neither* rotate case, so a rotated photograph read back
untransformed polygons and the segmentation silently detached from the anatomy while the
preview showed it correctly, which is the worse of the two arrangements: the person who
drew them saw them in the right place. ``common/fixtures/image_edit_replay.json`` is now
the contract both sides are tested against, so a change to one that is not matched in the
other fails on both.
"""

import math


def clone_teeth(teeth):
    """A rounded, string-keyed copy of a teeth map. The no-operations answer."""
    if not isinstance(teeth, dict):
        return {}
    cloned = {}
    for tooth_code, polygons in teeth.items():
        if not isinstance(polygons, list):
            continue
        cloned[str(tooth_code)] = [
            [[round(float(point[0]), 3), round(float(point[1]), 3)] for point in polygon]
            for polygon in polygons
            if isinstance(polygon, list)
        ]
    return cloned


def clip_polygon_to_rect(polygon, left, top, right, bottom):
    """Sutherland-Hodgman against a crop rectangle.

    Returns ``[]`` when fewer than three vertices survive, which is a real outcome rather
    than an error: a crop can remove a tooth from the picture entirely, and the honest
    record of that is no polygon.
    """

    def inside(point, edge):
        x, y = point
        if edge == "left":
            return x >= left
        if edge == "right":
            return x <= right
        if edge == "top":
            return y >= top
        return y <= bottom

    def intersect(start, end, edge):
        x1, y1 = start
        x2, y2 = end
        if edge in ("left", "right"):
            x = left if edge == "left" else right
            dx = x2 - x1
            if abs(dx) < 1e-9:
                return [x, y1]
            t = (x - x1) / dx
            return [x, y1 + (y2 - y1) * t]
        y = top if edge == "top" else bottom
        dy = y2 - y1
        if abs(dy) < 1e-9:
            return [x1, y]
        t = (y - y1) / dy
        return [x1 + (x2 - x1) * t, y]

    def clip_against(points, edge):
        output = []
        if not points:
            return output
        for idx, current in enumerate(points):
            previous = points[idx - 1]
            current_inside = inside(current, edge)
            previous_inside = inside(previous, edge)
            if current_inside:
                if not previous_inside:
                    output.append(intersect(previous, current, edge))
                output.append(current)
            elif previous_inside:
                output.append(intersect(previous, current, edge))
        return output

    result = [list(point) for point in polygon]
    for edge in ("left", "right", "top", "bottom"):
        result = clip_against(result, edge)
        if len(result) < 3:
            return []
    return [[round(point[0], 3), round(point[1], 3)] for point in result]


def apply_operation(point, operation):
    """One polygon vertex through one of the image editor's geometric operations.

    Must stay byte-for-byte equivalent to ``applyOperation`` in
    ``frontend/imaging/photos/editReplay.js`` -- see the module note for what happened
    when it was not.

    An unrecognised operation is the identity rather than an error: a reader that refused
    an operation it did not know would make the image unopenable instead of merely
    unrotated.
    """
    x = float(point[0])
    y = float(point[1])
    op_type = operation.get("type")
    if op_type == "flip-h":
        width = float(operation.get("input_width") or 0)
        return [round(width - x, 3), round(y, 3)]
    if op_type == "flip-v":
        height = float(operation.get("input_height") or 0)
        return [round(x, 3), round(height - y, 3)]
    if op_type == "crop":
        return [
            round(x - float(operation.get("x") or 0), 3),
            round(y - float(operation.get("y") or 0), 3),
        ]
    if op_type == "rotate-cw":
        # A quarter turn clockwise: the new x is measured down the old y axis from the
        # bottom, and the new y is the old x.
        height = float(operation.get("input_height") or 0)
        return [round(height - y, 3), round(x, 3)]
    if op_type == "rotate-arbitrary":
        theta = math.radians(((float(operation.get("angle") or 0) % 360) + 360) % 360)
        cx = float(operation.get("input_width") or 0) / 2
        cy = float(operation.get("input_height") or 0) / 2
        dx = x - cx
        dy = y - cy
        # The sign convention is the editor's, not a mathematical choice: it rotates about
        # the image centre into a bounding box that the crop then re-origins. Flipping
        # either term rotates the polygons the opposite way from the pixels, which looks
        # like a plausible segmentation of the wrong teeth.
        rot_x = (
            dx * math.cos(theta)
            + dy * math.sin(theta)
            + float(operation.get("bb_width") or 0) / 2
            - float(operation.get("crop_x") or 0)
        )
        rot_y = (
            -dx * math.sin(theta)
            + dy * math.cos(theta)
            + float(operation.get("bb_height") or 0) / 2
            - float(operation.get("crop_y") or 0)
        )
        return [round(rot_x, 3), round(rot_y, 3)]
    return [round(x, 3), round(y, 3)]


def transform_polygon(polygon, operations):
    """One ring through a whole operation list, in order.

    A crop clips *before* its own translation is applied, because the rectangle is
    expressed in the pre-crop frame.
    """
    transformed = [list(point) for point in polygon]
    for operation in operations or []:
        if len(transformed) < 3:
            return []
        if operation.get("type") == "crop":
            left = float(operation.get("x") or 0)
            top = float(operation.get("y") or 0)
            right = left + float(operation.get("width") or 0)
            bottom = top + float(operation.get("height") or 0)
            transformed = clip_polygon_to_rect(transformed, left, top, right, bottom)
            if len(transformed) < 3:
                return []
        transformed = [apply_operation(point, operation) for point in transformed]
    return transformed if len(transformed) >= 3 else []


def transform_teeth(teeth, edit_meta):
    """A whole teeth map through an ``edit_meta``.

    Replayed from the pristine geometry every time rather than composed onto its own
    output, which is what makes it idempotent -- the preview path depends on that, and
    without it polygons would drift a little further on every keystroke.

    :param edit_meta: ``{'operations': [...]}``, or anything falsy for "no edits".
    :returns: a new map; teeth whose polygons all vanish are dropped.
    """
    operations = edit_meta.get("operations") if isinstance(edit_meta, dict) else []
    if not operations:
        return clone_teeth(teeth)

    transformed_teeth = {}
    for tooth_code, polygons in (teeth or {}).items():
        next_polygons = []
        for polygon in polygons or []:
            transformed_polygon = transform_polygon(polygon, operations)
            if len(transformed_polygon) >= 3:
                next_polygons.append(transformed_polygon)
        if next_polygons:
            transformed_teeth[str(tooth_code)] = next_polygons
    return transformed_teeth
