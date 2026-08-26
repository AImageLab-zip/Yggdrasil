"""Translating maxillo's four legacy annotation representations.

Pure: legacy values in, descriptor dicts out. Nothing here queries, opens a file
or resolves a label.

The conversions are lossless on purpose -- decision #6 keeps the legacy tables
readable for one release as a cross-check, and a cross-check only means
something if the converted form carries everything the original did. Where a
legacy field has no home in the annotation model it goes into ``attributes``
rather than being dropped, so ``annotations_crosscheck`` can compare the two
representations field by field.
"""

import re

from django.core.exceptions import ValidationError

from annotations.adapters import descriptors
from annotations.constants import (
    CoordinateSystem,
    Geometry2DType,
    Geometry3DType,
    SliceAxis,
)

#: ``<patient_id>_<jaw>_FDI_<tooth>``, the key format
#: ``maxillo.views.patient_data`` validates on every save.
LANDMARK_KEY_RE = re.compile(r"^(\d+)_(upper|lower)_FDI_(\d{2})$")

#: Landmark entries holding a single point.
LANDMARK_POINT_TYPES = frozenset(
    {"incisal", "outer", "bracket", "gingival", "mesial", "distal", "inner", "facial"}
)
#: Landmark entries holding a list of points.
LANDMARK_MULTI_POINT_TYPES = frozenset({"cusps", "planar"})
#: The four named vectors of a tooth's base plane.
LANDMARK_PLANE_KEYS = ("origin", "xAxis", "yAxis", "zAxis")


def ios_landmarks(document, *, patient_id):
    """Convert the IOS landmark JSON document into 3D descriptors.

    The frame is ``resource_local``, not a patient frame. These points come out
    of ``worldToLocal`` against a specific mesh: they are coordinates in that
    mesh's own object space, and the mesh has no registration to the patient.
    Recording them as ``patient_lps_mm`` would be a false statement that a later
    fusion or export would act on.

    Each tooth's ``basePlane`` becomes one ``plane`` item from its origin and
    two axis endpoints, with the third axis kept in ``attributes`` -- the model
    stores a plane as three points, and z is derivable but not identical to a
    recomputed cross product once floats are involved, so it is preserved rather
    than reconstructed.
    """
    if not isinstance(document, dict):
        raise ValidationError("the landmark document must be a JSON object")

    out = []
    for key in sorted(document):
        match = LANDMARK_KEY_RE.match(str(key))
        if not match:
            raise ValidationError(f"landmark key {key!r} is not in the FDI format")
        if match.group(1) != str(patient_id):
            raise ValidationError(
                f"landmark key {key!r} belongs to patient {match.group(1)}, not {patient_id}"
            )
        jaw, tooth = match.group(2), match.group(3)
        entry = document[key]
        if not isinstance(entry, dict):
            raise ValidationError(f"landmark entry {key!r} must be an object")

        shared = {"jaw": jaw, "fdi": tooth, "legacy_key": key}

        for name in sorted(LANDMARK_POINT_TYPES & set(entry)):
            out.append(
                descriptors.spatial_3d(
                    geometry_type=Geometry3DType.POINT,
                    coordinate_system=CoordinateSystem.RESOURCE_LOCAL,
                    points=[list(entry[name])],
                    label_code=tooth,
                    attributes={**shared, "landmark": name},
                )
            )

        for name in sorted(LANDMARK_MULTI_POINT_TYPES & set(entry)):
            points = entry[name]
            if not isinstance(points, list):
                raise ValidationError(f"{key}.{name} must be a list of points")
            # One item per point rather than a polyline: cusps are unordered
            # landmarks that happen to be stored in a list, and a polyline would
            # assert an order and a connectivity the original never had.
            for index, point in enumerate(points):
                out.append(
                    descriptors.spatial_3d(
                        geometry_type=Geometry3DType.POINT,
                        coordinate_system=CoordinateSystem.RESOURCE_LOCAL,
                        points=[list(point)],
                        label_code=tooth,
                        order=index,
                        attributes={**shared, "landmark": name, "index": index},
                    )
                )

        plane = entry.get("basePlane")
        if plane is not None:
            if not isinstance(plane, dict) or set(plane) != set(LANDMARK_PLANE_KEYS):
                raise ValidationError(
                    f"{key}.basePlane must hold exactly {list(LANDMARK_PLANE_KEYS)}"
                )
            out.append(
                descriptors.spatial_3d(
                    geometry_type=Geometry3DType.PLANE,
                    coordinate_system=CoordinateSystem.RESOURCE_LOCAL,
                    points=[
                        list(plane["origin"]),
                        list(plane["xAxis"]),
                        list(plane["yAxis"]),
                    ],
                    label_code=tooth,
                    attributes={**shared, "landmark": "basePlane", "zAxis": list(plane["zAxis"])},
                )
            )

    return out


def intraoral_segmentation(teeth):
    """Convert ``IntraoralToothSegmentation.teeth`` into 2D polygon descriptors.

    The legacy shape is ``{FDI: [[[x, y], ...], ...]}`` -- a tooth may own
    several disjoint polygons, which is why the inner list exists and why each
    one becomes its own item rather than being merged.

    Coordinates are image pixels. They are *not* normalized on the way in: the
    photograph they were drawn on is the resource, and rescaling them here would
    make the converted form differ from the original for no reason a
    cross-check could explain.
    """
    if not isinstance(teeth, dict):
        raise ValidationError("teeth must be a JSON object keyed by FDI code")

    out = []
    for fdi in sorted(teeth):
        polygons = teeth[fdi]
        if not isinstance(polygons, list):
            raise ValidationError(f"tooth {fdi} must hold a list of polygons")
        for index, polygon in enumerate(polygons):
            if not isinstance(polygon, list):
                raise ValidationError(f"tooth {fdi} polygon {index} must be a list")
            out.append(
                descriptors.geometry_2d(
                    geometry_type=Geometry2DType.POLYGON,
                    coordinate_system=CoordinateSystem.IMAGE_PIXEL,
                    points=[list(point) for point in polygon],
                    closed=True,
                    label_code=str(fdi),
                    order=index,
                    attributes={"fdi": str(fdi), "polygon_index": index},
                )
            )
    return out


def panoramic_arch(spline, *, axial_slice, volume_shape, geometry_source, default_mode, algorithm_version=""):
    """Convert a ``PanoramicState`` arch into one 2D polyline descriptor.

    The frame is ``slice_pixel``, with a slice selector carrying the axial
    index. That pairing is enforced by the validators, and it is the point: a
    spline is a list of ``[x, y]`` pairs inside *one* axial slice of the volume,
    and without the index it is a curve nobody can place.

    An ``auto`` arch is still converted. It is not human annotation work -- the
    caller records it with a prediction origin, so it never sets
    ``ever_annotated`` and never locks a case -- but it is the geometry the
    baked strips were produced from, and dropping it would leave the exported
    PNGs unexplained.
    """
    if isinstance(spline, dict):
        control_points = spline.get("control_points") or spline.get("controlPoints")
    else:
        control_points = spline
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


#: The occlusion facets, in the order the sidebar shows them.
OCCLUSION_FACETS = (
    "sagittal_left",
    "sagittal_right",
    "vertical",
    "transverse",
    "midline",
)


def occlusion_classification(values, *, classifier="manual"):
    """Convert a ``Classification`` row into one event descriptor per facet.

    Five rows rather than five columns, so a sixth facet needs no migration.
    ``Unknown`` is carried across rather than skipped: it is the value the form
    writes for "not yet assessed", and treating it as absent would make a
    reviewed-but-inconclusive case indistinguishable from an untouched one.
    """
    out = []
    for order, facet in enumerate(OCCLUSION_FACETS):
        value = values.get(facet)
        if value is None:
            continue
        out.append(
            descriptors.event(
                event_type=f"occlusion.{facet}",
                value=str(value),
                order=order,
                attributes={"classifier": classifier},
            )
        )
    if not out:
        raise ValidationError("a classification with no facets carries no statement")
    return out
