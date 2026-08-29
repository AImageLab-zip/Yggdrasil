"""Measurements as a DICOM Structured Report (TID 1500).

Decision #11 named this: "first release also wires ROI stats + calibration + DICOM SR
export". The calibration half is the reason this module is careful.

**An uncalibrated measurement must survive the trip as uncalibrated.**
``MeasurementItem`` exists to keep that distinction -- a length taken on an image with
no known pixel spacing is a number of *pixels*, and the database refuses to let it wear
a millimetre unit. The naive encodings are to convert, or to write ``mm`` and hope; both
invent a physical claim the image cannot support. So the **unit carries it**: an
uncalibrated length is written with UCUM ``{pixels}``, a curly-brace *annotation* unit,
which is exactly what UCUM provides for a dimensionless count that needs a name.

The first draft of this module also set a "not calibrated" code as the measurement's
``qualifier``. That was wrong and is worth recording rather than quietly removing.
``highdicom`` maps ``qualifier`` onto **Numeric Value Qualifier (0040,A301)**, whose
defined terms are CID 42 -- "value out of range", "measurement failure", "not a
number", the infinities. It qualifies a value as *unusable*. Putting a calibration
statement there tells a conforming receiver the number is not a number, which is a
worse lie than the one it was trying to prevent, and a strict receiver may reject the
document outright. There is no standard coded concept for "this measurement is
uncalibrated", and inventing a private one puts a string in an interchange document
that only this repository can read. The unit is the honest carrier, and it is
unambiguous: ``{pixels}`` cannot be mistaken for a millimetre by anything.

**Comprehensive3DSR, not ComprehensiveSR.** The grid's measurements are anchored in
patient LPS millimetres against a volume, not to a pixel region of one slice, so their
coordinates are ``SCOORD3D`` and the SOP class that may carry those is the 3D one.
Writing the 2D class would mean projecting every measurement onto whichever slice
happened to be showing, which is not where the user drew it.
"""

import logging

import highdicom as hd
import numpy as np
from pydicom.sr.codedict import codes
from pydicom.sr.coding import Code

from annotations.constants import (
    CoordinateSystem,
    Geometry3DType,
    MeasurementKind,
    MeasurementUnit,
)
from common.interop.sources import InteropUnavailable, derived_uid

logger = logging.getLogger(__name__)

#: What each stored measurement *is*, as a coded concept. Every one of these is a
#: standard term; nothing here is invented, because a private concept code in an
#: interchange document is a string only this repository can read.
_CONCEPTS = {
    MeasurementKind.LENGTH: codes.SCT.Length,
    MeasurementKind.PERIMETER: codes.SCT.Perimeter,
    MeasurementKind.DIAMETER: codes.SCT.Diameter,
    MeasurementKind.ANGLE: Code("814161000000106", "SCT", "Angle"),
    MeasurementKind.AREA: codes.SCT.Area,
    MeasurementKind.VOLUME: codes.SCT.Volume,
    MeasurementKind.MEAN: Code("373098007", "SCT", "Mean"),
    MeasurementKind.STDDEV: Code("386136009", "SCT", "Standard Deviation"),
    MeasurementKind.MIN: Code("255605001", "SCT", "Minimum"),
    MeasurementKind.MAX: Code("56851009", "SCT", "Maximum"),
    MeasurementKind.COUNT: Code("246205007", "SCT", "Quantity"),
}

#: The unit each stored unit becomes. ``{pixels}`` and ``{ratio}`` are UCUM annotation
#: units -- legal, dimensionless, and honest about being a count rather than a size.
#: The alternative, converting to millimetres, is the exact fabrication
#: ``MeasurementItem.is_calibrated`` exists to prevent.
_UNITS = {
    MeasurementUnit.MM: codes.UCUM.Millimeter,
    MeasurementUnit.MM2: codes.UCUM.SquareMillimeter,
    MeasurementUnit.MM3: Code("mm3", "UCUM", "cubic millimeter"),
    MeasurementUnit.PX: Code("{pixels}", "UCUM", "pixels"),
    MeasurementUnit.PX2: Code("{pixels2}", "UCUM", "square pixels"),
    MeasurementUnit.PX3: Code("{pixels3}", "UCUM", "cubic pixels"),
    MeasurementUnit.DEG: Code("deg", "UCUM", "degree"),
    MeasurementUnit.HU: codes.UCUM.HounsfieldUnit,
    MeasurementUnit.NONE: codes.UCUM.NoUnits,
}

#: How a stored 3D shape is drawn in SCOORD3D. ``SPHERE`` has no SCOORD3D graphic type,
#: so it is written as its centre POINT and the radius stays in the measurement that
#: was computed from it -- a lossy encoding, stated rather than hidden.
_GRAPHIC_TYPES = {
    Geometry3DType.POINT: hd.sr.GraphicTypeValues3D.POINT,
    Geometry3DType.POLYLINE: hd.sr.GraphicTypeValues3D.POLYLINE,
    Geometry3DType.PLANE: hd.sr.GraphicTypeValues3D.POLYGON,
    Geometry3DType.BOX: hd.sr.GraphicTypeValues3D.MULTIPOINT,
    Geometry3DType.SPHERE: hd.sr.GraphicTypeValues3D.POINT,
}


def _observation_context(series):
    """Who observed this. A device, because the export is not a person.

    The device UID is derived from the series rather than random, so two exports of
    one study name the same observer and a receiving system does not accumulate a new
    "observer" per download.
    """
    return hd.sr.ObservationContext(
        observer_device_context=hd.sr.ObserverContext(
            observer_type=codes.DCM.Device,
            observer_identifying_attributes=hd.sr.DeviceObserverIdentifyingAttributes(
                uid=derived_uid("sr-observer", series.series_instance_uid),
                manufacturer_name="Yggdrasil",
                model_name="Yggdrasil annotation export",
            ),
        )
    )


def _coordinates(spatial_item, frame_of_reference_uid):
    """The SCOORD3D a measurement was computed from, or ``None``.

    ``None`` for a measurement with no shape -- a whole-volume mean HU is a real
    measurement of nothing in particular, which ``MeasurementItem`` allows on purpose.
    """
    if spatial_item is None or not spatial_item.points:
        return None
    if spatial_item.coordinate_system != CoordinateSystem.PATIENT_LPS_MM:
        # SCOORD3D is defined in the frame of reference, which is patient LPS. A
        # RAS-stored shape differs by two sign flips: converting it here silently
        # would mirror the shape across two planes, so it is refused instead.
        logger.warning(
            "Measurement geometry %s is in %s, not patient LPS; omitted from the SR",
            spatial_item.pk,
            spatial_item.coordinate_system,
        )
        return None
    graphic_type = _GRAPHIC_TYPES.get(spatial_item.geometry_type)
    if graphic_type is None:
        return None
    points = np.asarray(spatial_item.points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3:
        return None
    if graphic_type == hd.sr.GraphicTypeValues3D.POINT:
        points = points[:1]
    if graphic_type == hd.sr.GraphicTypeValues3D.POLYGON and len(points) < 3:
        return None
    return hd.sr.CoordinatesForMeasurement3D(
        graphic_type=graphic_type,
        graphic_data=points,
        frame_of_reference_uid=frame_of_reference_uid,
    )


def _measurement(item, frame_of_reference_uid):
    """One ``MeasurementItem`` as a TID 300 measurement, or ``None`` if it cannot be."""
    concept = _CONCEPTS.get(item.kind)
    unit = _UNITS.get(item.unit)
    if concept is None or unit is None:
        logger.warning(
            "No DICOM concept for measurement %s (%s in %s); omitted from the SR",
            item.pk,
            item.kind,
            item.unit,
        )
        return None
    coordinates = _coordinates(item.spatial_3d_item, frame_of_reference_uid)
    return hd.sr.Measurement(
        name=concept,
        value=float(item.value),
        unit=unit,
        tracking_identifier=hd.sr.TrackingIdentifier(
            uid=derived_uid("sr-measurement", item.pk),
            identifier=item.label.code if item.label_id else f"measurement-{item.pk}",
        ),
        referenced_coordinates=[coordinates] if coordinates is not None else None,
    )


def build_sr(revision, series, source_datasets):
    """One revision's measurements as a Comprehensive3DSR, or ``None`` if it has none.

    :param revision: the ``AnnotationRevision`` to render.
    :param series: the ``DicomSeries`` the annotations are anchored to.
    :param source_datasets: that series' instances, from
        :func:`common.interop.sources.instance_datasets`.
    :returns: a ``pydicom`` dataset, or ``None`` when nothing survived encoding.
    """
    frame_of_reference_uid = (series.frame_of_reference_uid or "").strip()
    if not frame_of_reference_uid:
        # Without one, SCOORD3D coordinates name a space nobody can identify. A CT or
        # CBCT always declares it; a series that does not is refused rather than given
        # a fabricated frame, which is the same rule ingest applies to orientation.
        raise InteropUnavailable(
            f"DICOM series {series.series_instance_uid} declares no Frame of Reference, "
            "so its measurements cannot be expressed as SCOORD3D"
        )

    items = list(
        revision.measurementitems.select_related("spatial_3d_item", "label").all()
    )
    measurements = [
        measurement
        for measurement in (_measurement(item, frame_of_reference_uid) for item in items)
        if measurement is not None
    ]
    if not measurements:
        return None

    group = hd.sr.MeasurementsAndQualitativeEvaluations(
        tracking_identifier=hd.sr.TrackingIdentifier(
            uid=derived_uid("sr-group", series.series_instance_uid, revision.pk),
            identifier=f"Yggdrasil revision {revision.revision_number}",
        ),
        measurements=measurements,
    )
    report = hd.sr.MeasurementReport(
        observation_context=_observation_context(series),
        procedure_reported=codes.SCT.ImagingProcedure,
        imaging_measurements=[group],
    )
    return hd.sr.Comprehensive3DSR(
        evidence=source_datasets,
        content=report[0],
        series_instance_uid=derived_uid("sr-series", series.series_instance_uid),
        series_number=1,
        sop_instance_uid=derived_uid(
            "sr-instance", series.series_instance_uid, revision.pk
        ),
        instance_number=1,
        manufacturer="Yggdrasil",
        is_complete=True,
        is_final=True,
    )
