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

from django.core.exceptions import ValidationError

from annotations.adapters import descriptors
from annotations.adapters import ios_landmarks as _ios
from annotations.adapters import panoramic as _panoramic
from annotations.adapters.tooth_segmentation import tooth_polygons

#: Re-exported from :mod:`annotations.adapters.ios_landmarks`, which owns the landmark
#: conversion now that the live editor calls it too. Kept importable from here because
#: ``annotations_materialize_landmarks`` and the existing tests reach for them by this path.
LANDMARK_KEY_RE = _ios.LANDMARK_KEY_RE
LANDMARK_POINT_TYPES = _ios.LANDMARK_POINT_TYPES
LANDMARK_MULTI_POINT_TYPES = _ios.LANDMARK_MULTI_POINT_TYPES
LANDMARK_PLANE_KEYS = _ios.LANDMARK_PLANE_KEYS


def ios_landmarks(document, *, patient_id):
    """Convert the IOS landmark JSON document into 3D descriptors.

    Delegates to :func:`annotations.adapters.ios_landmarks.ios_landmarks`, which is also
    what the live editor calls. That is the point rather than tidiness: decision #6 keeps
    the legacy artifact readable for one release as a cross-check, and
    ``annotations_crosscheck`` compares the two representations. Two implementations of
    this conversion would drift, and the drift would surface as the cross-check reporting
    differences on every study anybody had edited -- burying the signal it exists to give.

    The frame is ``resource_local``, not a patient frame; the reasoning is in that module.
    """
    return _ios.ios_landmarks(document, patient_id=patient_id)


def intraoral_segmentation(teeth):
    """Convert ``IntraoralToothSegmentation.teeth`` into 2D polygon descriptors.

    Delegates to :func:`annotations.adapters.tooth_segmentation.tooth_polygons`, which is
    also what the live editor calls. That is the point rather than tidiness: decision #6
    keeps the legacy table readable for one release as a cross-check, and
    ``annotations_crosscheck`` compares the two representations field by field. Two
    implementations of this conversion would drift, and the drift would surface as the
    cross-check reporting differences on every study anybody had edited -- burying the
    signal it exists to give.
    """
    return tooth_polygons(teeth)


def panoramic_arch(spline, *, axial_slice, volume_shape, geometry_source, default_mode, algorithm_version=""):
    """Convert a ``PanoramicState`` arch into one 2D polyline descriptor.

    Delegates to :func:`annotations.adapters.panoramic.panoramic_arch`, which is also what
    the live editor calls. That is the point rather than tidiness: decision #6 keeps the
    legacy table readable for one release as a cross-check, and ``annotations_crosscheck``
    compares the two representations. Two implementations of this conversion would drift,
    and the drift would surface as the cross-check reporting differences on every study
    anybody had edited -- burying the signal it exists to give.

    The frame is ``slice_pixel`` with a slice selector carrying the axial index; the
    reasoning is in that module.
    """
    return _panoramic.panoramic_arch(
        spline,
        axial_slice=axial_slice,
        volume_shape=volume_shape,
        geometry_source=geometry_source,
        default_mode=default_mode,
        algorithm_version=algorithm_version,
    )


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
