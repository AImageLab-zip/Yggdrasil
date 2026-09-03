"""Normalising a tooth-polygon payload that came from outside.

What is left of ``maxillo/views/intraoral_segmentation.py`` after Phase 5 replaced the
Konva editor. Its two endpoints are gone -- the editor now saves through
``annotations.services.segmentation`` -- and so is the module's own copy of the image-edit
replay, which moved to :mod:`annotations.adapters.image_edit_replay` where both the read
path and the export can reach it.

Not a views module any more, hence the move out of ``views/``. What remains is one job: take
a teeth map from the segmentation pipeline's ``segmentation_json`` and turn it into
something storable, **dropping** what cannot be stored rather than refusing the lot.

That is the difference from ``annotations.adapters.tooth_segmentation.tooth_polygons``,
which *refuses* a malformed map, and both behaviours are right for their caller. A person
drawing gets a refusal, because a polygon quietly discarded is work they think they did. A
model's output gets a filter, because one two-point ring in a batch of thirty should not
fail the whole job completion -- which is how it has always behaved.
"""

import math

#: Permanent dentition, in the order the old editor's grid drew them. The set is what this
#: module actually uses; the order is kept because it is the arch as a clinician reads it.
TOOTH_CODES = [
    '18', '17', '16', '15', '14', '13', '12', '11',
    '21', '22', '23', '24', '25', '26', '27', '28',
    '48', '47', '46', '45', '44', '43', '42', '41',
    '31', '32', '33', '34', '35', '36', '37', '38',
]
TOOTH_CODE_SET = set(TOOTH_CODES)

#: Per *polygon*, despite the name, which is kept because
#: ``annotations.adapters.tooth_segmentation`` mirrors it and cites it. Not a performance
#: guard: a pipeline that appended to a ring in a loop would otherwise write unboundedly.
MAX_POINTS_PER_TOOTH = 500


def _is_point(value):
    return (
        isinstance(value, list)
        and len(value) >= 2
        and not isinstance(value[0], (list, tuple))
        and not isinstance(value[1], (list, tuple))
    )


def _normalize_polygon(points, image_bounds=None):
    if not isinstance(points, list):
        raise ValueError('Polygon must be a list of points.')

    if len(points) < 3:
        raise ValueError('Polygon must have at least 3 points.')
    if len(points) > MAX_POINTS_PER_TOOTH:
        raise ValueError(f'Polygon exceeds {MAX_POINTS_PER_TOOTH} points.')

    normalized = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError('Each point must be [x, y].')

        x = float(point[0])
        y = float(point[1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError('Point coordinates must be finite numbers.')
        if image_bounds:
            width, height = image_bounds
            if x < 0 or y < 0 or x > width or y > height:
                raise ValueError('Point coordinates must stay inside image bounds.')

        normalized.append([round(x, 3), round(y, 3)])

    return normalized


def _normalize_polygons(value, image_bounds=None):
    if not isinstance(value, list):
        raise ValueError('Polygon set must be a list.')
    if not value:
        return []
    # The bare single-polygon shape, `{"11": [[x, y], ...]}`. Tolerated here and refused by
    # the adapter; a production count found 0 of 5,491 rows using it, so the tolerance is
    # for pipeline output rather than for the stored corpus.
    if _is_point(value[0]):
        return [_normalize_polygon(value, image_bounds)]
    return [_normalize_polygon(polygon, image_bounds) for polygon in value if polygon]


def _normalize_teeth_payload(teeth_payload, image_bounds=None):
    if teeth_payload in (None, ''):
        return {}
    if not isinstance(teeth_payload, dict):
        raise ValueError('Teeth payload must be an object.')

    normalized = {}
    for tooth_code, polygon in teeth_payload.items():
        code = str(tooth_code).strip()
        if code not in TOOTH_CODE_SET:
            raise ValueError(f'Unsupported tooth code: {code}')
        polygons = _normalize_polygons(polygon, image_bounds)
        if polygons:
            normalized[code] = polygons

    return normalized
