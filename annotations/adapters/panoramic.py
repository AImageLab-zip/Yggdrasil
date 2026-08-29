"""Translating the panoramic arch, in both directions.

The arch is one open polyline of control points lying in a single axial slice of a CBCT.
It is not a clinical finding: it is the geometry the MIP and ray-sum strips were baked
from, and decision #8 requires those strips to stay exportable. Converting it is what
keeps the exported PNGs explained by something other than a filename.

**One implementation, two callers.** ``annotations_convert_legacy`` reads
``maxillo.PanoramicState`` rows and the live editor posts a fresh arch; both arrive here.
Decision #6 keeps the legacy table readable for one release as a cross-check, and a
cross-check only means something if the two representations were produced by the same
code. Two conversions would drift, and the drift would surface as
``annotations_crosscheck`` reporting a difference on every study anybody had edited --
burying the signal it exists to give. ``legacy_maxillo.panoramic_arch`` delegates here for
exactly that reason, the way tooth segmentation and IOS landmarks already do.

**The frame is ``slice_pixel``, and the slice lives on the selector.** A spline is a list
of ``[x, y]`` pairs inside *one* axial slice; without the index it is a curve nobody can
place. ``validate_item_selector_pairing`` refuses the combination that would lose it.
"""

from django.core.exceptions import ValidationError

from annotations.adapters import descriptors
from annotations.constants import CoordinateSystem, Geometry2DType, SliceAxis

#: The ``AnnotationSet.kind`` the arch is filed under. Already a choice in migration
#: ``0001``, and already what ``annotations_convert_legacy`` writes.
PANORAMIC_KIND = "panoramic_arch"


def _control_points(spline):
    """The ``[[x, y], ...]`` list out of either shape a caller may hold.

    ``PanoramicState.spline`` is a bare list; the browser posts either that or
    ``{"control_points": [...]}``. Accepting both here rather than at each call site is
    what lets the converter and the live save share one function.
    """
    if isinstance(spline, dict):
        return spline.get("control_points") or spline.get("controlPoints")
    return spline


def panoramic_arch(
    spline,
    *,
    axial_slice,
    volume_shape,
    geometry_source,
    default_mode,
    algorithm_version="",
):
    """Convert an arch into the single 2D polyline descriptor that represents it.

    An ``auto`` arch is converted like any other. It is not human annotation work -- the
    caller records it with a prediction origin, so it never sets ``ever_annotated`` and
    never locks a case -- but it is the geometry the baked strips came from, and dropping
    it would leave the exported PNGs unexplained.

    :returns: a one-element descriptor list, so callers can splice it like any other.
    """
    control_points = _control_points(spline)
    if not isinstance(control_points, list) or len(control_points) < 2:
        raise ValidationError("a panoramic arch needs at least two control points")

    selector = descriptors.slice_selector(
        axis=SliceAxis.AXIAL,
        index=axial_slice,
        coordinate_system=CoordinateSystem.SLICE_PIXEL,
    )
    return [
        descriptors.geometry_2d(
            geometry_type=Geometry2DType.POLYLINE,
            coordinate_system=CoordinateSystem.SLICE_PIXEL,
            points=[list(point) for point in control_points],
            closed=False,
            selector=selector,
            attributes={
                "volume_shape": list(volume_shape or []),
                "geometry_source": geometry_source,
                "default_mode": default_mode,
                "algorithm_version": algorithm_version,
            },
        )
    ]


def arch_from_items(items):
    """The inverse: canonical rows back into the arch the editor reads.

    Rebuilt from the *items*, not from a payload. The control points **are** the arch --
    there is no handle convention the model cannot express -- so a scratch copy would add
    nothing but a second thing to keep in step. The same argument tooth segmentation
    makes, for the same reason.

    A revision holds exactly one arch, so the first polyline wins; a revision holding
    none returns ``None`` rather than an empty arch, because "this study has no arch" and
    "this study has an arch with no points" are different answers and only the first one
    is real.

    :param items: ``Geometry2DItem`` rows, or anything with ``points``, ``attributes`` and
        a ``selector``.
    :returns: ``{"spline", "axial_slice", "volume_shape", "geometry_source",
        "default_mode", "algorithm_version"}``, or ``None``.
    """
    for item in items:
        if getattr(item, "geometry_type", Geometry2DType.POLYLINE) != Geometry2DType.POLYLINE:
            continue
        attributes = item.attributes if isinstance(item.attributes, dict) else {}
        selector = getattr(item, "selector", None)
        return {
            "spline": [list(point) for point in (item.points or [])],
            # From the selector, never from the attributes: the selector is where the
            # validators guarantee it exists, and a second copy would be the one that
            # goes stale.
            "axial_slice": getattr(selector, "slice_index", None),
            "volume_shape": list(attributes.get("volume_shape") or []),
            "geometry_source": attributes.get("geometry_source") or "",
            "default_mode": attributes.get("default_mode") or "",
            "algorithm_version": attributes.get("algorithm_version") or "",
        }
    return None
