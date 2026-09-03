"""FDI-keyed tooth polygons in, descriptors out.

The canonical conversion, and the one this module exists to be: both the live editor and
``annotations_convert_legacy`` go through it, so what the converter wrote and what the
viewer writes are the same rows by construction rather than by two people agreeing.

That matters concretely. Decision #6 keeps the legacy
``maxillo.IntraoralToothSegmentation`` table readable for one release as a cross-check,
and ``annotations_crosscheck`` compares the two representations field by field. If the
live path produced even a slightly different shape -- points as tuples instead of lists,
a different ``order``, a missing ``fdi`` attribute -- the cross-check would report drift
on every study anybody had edited, and the real signal would be lost in it.

Pure: values in, descriptor dicts out. Nothing here queries or resolves a label; the FDI
code travels as ``label_code`` and :mod:`annotations.services.apply` looks it up in the
set's own schema. That lookup is where an unknown code is *refused* rather than written
unlabelled, which is the behaviour the roadmap calls load-bearing -- a polygon with no
label is exported under the wrong segment number and looks fine doing it.
"""

from django.core.exceptions import ValidationError

from annotations.adapters import descriptors
from annotations.constants import CoordinateSystem, Geometry2DType

#: The set kind these polygons are filed under. Already a valid ``AnnotationSet.kind``
#: and already what the legacy converter uses -- reusing it is what lets the cross-check
#: find both representations of one study.
SEGMENTATION_KIND = "intraoral_segmentation"

#: The vocabulary the FDI codes resolve against, seeded by ``migrations/0002``.
FDI_SCHEMA_SLUG = "fdi-permanent"
FDI_SCHEMA_VERSION = 1

#: A tooth with more polygons than this, or a polygon with more points, is refused.
#:
#: Mirrors ``MAX_POINTS_PER_TOOTH`` in ``maxillo/views/intraoral_segmentation.py``, which
#: despite its name is per *polygon*. Not a performance guard: a client bug that appends
#: to a polygon in a loop would otherwise turn a stuck mouse into an unbounded write.
MAX_POINTS_PER_POLYGON = 500
MAX_POLYGONS_PER_TOOTH = 32


def tooth_polygons(teeth):
    """Convert ``{FDI: [[[x, y], ...], ...]}`` into 2D polygon descriptors.

    A tooth may own several disjoint polygons -- a molar split by a restoration, a crown
    visible either side of an obstruction -- which is why the inner list exists and why
    each polygon becomes its own item rather than being merged into one ring.

    Coordinates are image pixels and are **not** normalised. The photograph they were
    drawn on is the resource, so rescaling them here would make the stored form differ
    from what the user drew for no reason a cross-check could explain.

    :param teeth: the FDI-keyed map.
    :returns: a list of descriptors, ordered by FDI code then polygon index, so two
        conversions of one study produce byte-identical output.
    :raises ValidationError: on a malformed map. Refusing beats storing a shape nobody
        can read back.
    """
    if not isinstance(teeth, dict):
        raise ValidationError("teeth must be a JSON object keyed by FDI code")

    out = []
    for fdi in sorted(teeth):
        polygons = teeth[fdi]
        if not isinstance(polygons, list):
            raise ValidationError(f"tooth {fdi} must hold a list of polygons")
        if len(polygons) > MAX_POLYGONS_PER_TOOTH:
            raise ValidationError(
                f"tooth {fdi} has {len(polygons)} polygons, more than the "
                f"{MAX_POLYGONS_PER_TOOTH} limit; this is a client bug rather than an "
                "anatomy nobody has seen"
            )
        for index, polygon in enumerate(polygons):
            if not isinstance(polygon, list):
                raise ValidationError(f"tooth {fdi} polygon {index} must be a list")
            if len(polygon) > MAX_POINTS_PER_POLYGON:
                raise ValidationError(
                    f"tooth {fdi} polygon {index} has {len(polygon)} points, more than "
                    f"the {MAX_POINTS_PER_POLYGON} limit"
                )
            out.append(
                descriptors.geometry_2d(
                    geometry_type=Geometry2DType.POLYGON,
                    coordinate_system=CoordinateSystem.IMAGE_PIXEL,
                    points=[list(point) for point in polygon],
                    # A polygon is closed by definition, and the validator says so: an
                    # open ring is a polyline and means something else.
                    closed=True,
                    label_code=str(fdi),
                    order=index,
                    # `fdi` duplicates the label deliberately: the label is a foreign key
                    # that a schema migration could in principle re-point, and a
                    # cross-check needs the code the user actually drew under.
                    attributes={"fdi": str(fdi), "polygon_index": index},
                )
            )
    return out


def teeth_from_items(items):
    """The inverse: canonical rows back into the map the editor reads.

    Rebuilt from the *items*, not from a payload, because for this surface the items are
    the only representation -- unlike a measurement, a tooth polygon has no handle
    convention the model cannot express, so there is nothing a scratch copy would add.

    :param items: ``Geometry2DItem`` rows, or anything with ``points``, ``order`` and an
        ``attributes['fdi']``.
    :returns: ``{FDI: [[[x, y], ...], ...]}`` with each tooth's polygons in ``order``.
    """
    grouped = {}
    for item in items:
        attributes = item.attributes if isinstance(item.attributes, dict) else {}
        # The label is authoritative when present; `attributes['fdi']` is the fallback
        # for a row written before the label existed.
        fdi = None
        if getattr(item, "label", None) is not None and item.label.code:
            fdi = str(item.label.code)
        elif attributes.get("fdi"):
            fdi = str(attributes["fdi"])
        if not fdi:
            continue
        grouped.setdefault(fdi, []).append((attributes.get("polygon_index", item.order), item.points))

    return {
        fdi: [points for _, points in sorted(entries, key=lambda pair: pair[0])]
        for fdi, entries in sorted(grouped.items())
    }
