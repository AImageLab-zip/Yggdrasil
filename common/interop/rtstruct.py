"""Stored contours as a DICOM RT Structure Set.

Written against ``pydicom`` directly. The roadmap's risk 13 assumed ``highdicom`` had an
RTSTRUCT writer that was merely under-exercised; it has none at all (0.28.1 ships ``seg``,
``sr``, ``pm``, ``ann``, ``ko``, ``pr``, ``sc`` and ``legacy``). The RT Structure Set IOD
is small and completely specified, so it is built attribute by attribute here and
``common/tests_interop.py`` reads every object back with ``pydicom``, re-derives the
contour points and compares them to the stored items. That round trip is a stronger claim
than "we used a library" would have been, and it is the only claim this module makes: the
proof is a fixture round trip, not a PACS.

**What an RTSTRUCT can and cannot carry, stated because the difference is lossy.** A
structure set is a stack of *planar* contours -- each ``ContourData`` is a list of
coplanar points with a referenced image. Yggdrasil's ``SpatialAnnotation3DItem`` holds
polylines, planes, boxes, spheres and points in patient LPS. Polylines and planes map
directly (``CLOSED_PLANAR``/``OPEN_PLANAR``). A point maps to ``POINT``. A **box** and a
**sphere** do not map at all: DICOM has no such ROI primitive, and approximating a sphere
by tessellating it into planar contours would file a rendering choice as clinical data.
They are skipped, with a log line naming the item, rather than approximated.
"""

import logging
from datetime import datetime

import numpy as np
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
# Deliberately not ``pydicom.uid.generate_uid``: every UID this package writes is
# derived, so re-exporting a study twice produces the same object rather than a new
# one a receiving system would file alongside the first.
from pydicom.uid import ExplicitVRLittleEndian

from annotations.constants import CoordinateSystem, Geometry3DType
from common.interop.sources import InteropUnavailable, derived_uid

logger = logging.getLogger(__name__)

#: SOP Class UID for RT Structure Set Storage.
RT_STRUCTURE_SET_STORAGE = "1.2.840.10008.5.1.4.1.1.481.3"

#: Which geometry types become which ``ContourGeometricType``. Absent from this map
#: means "cannot be expressed", not "use a default" -- see the module docstring.
_CONTOUR_TYPES = {
    Geometry3DType.POLYLINE: "OPEN_PLANAR",
    Geometry3DType.PLANE: "CLOSED_PLANAR",
    Geometry3DType.POINT: "POINT",
}


def _contour_points(item):
    """The item's points as an (N, 3) float array in patient LPS, or ``None``.

    A RAS-stored shape is refused rather than converted: LPS and RAS differ by two sign
    flips, so a silent conversion here would mirror the ROI across two planes and the
    result would look plausible. ``CoordinateSystem`` names the frame on every row
    precisely so this check is possible.
    """
    if item.coordinate_system != CoordinateSystem.PATIENT_LPS_MM:
        logger.warning(
            "3D item %s is in %s, not patient LPS; omitted from the RTSTRUCT",
            item.pk,
            item.coordinate_system,
        )
        return None
    points = np.asarray(item.points or [], dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or len(points) == 0:
        return None
    return points


def _referenced_frame_of_reference(series, source_datasets):
    """The one sequence that ties every ROI to the images it was drawn on."""
    contour_images = []
    for dataset in source_datasets:
        image = Dataset()
        image.ReferencedSOPClassUID = dataset.SOPClassUID
        image.ReferencedSOPInstanceUID = dataset.SOPInstanceUID
        contour_images.append(image)

    series_item = Dataset()
    series_item.SeriesInstanceUID = series.series_instance_uid
    series_item.ContourImageSequence = contour_images

    study_item = Dataset()
    study_item.ReferencedSOPClassUID = "1.2.840.10008.3.1.2.3.1"  # Detached Study Mgmt
    study_item.ReferencedSOPInstanceUID = series.study_instance_uid
    study_item.RTReferencedSeriesSequence = [series_item]

    frame_item = Dataset()
    frame_item.FrameOfReferenceUID = series.frame_of_reference_uid
    frame_item.RTReferencedStudySequence = [study_item]
    return [frame_item]


def _roi_datasets(items, series, source_datasets):
    """The three parallel sequences an RTSTRUCT keys together by ``ROINumber``.

    They are built in one pass rather than three because the IOD requires them to agree
    element for element: a structure set whose ``ROIContourSequence`` names an ROI its
    ``StructureSetROISequence`` does not is malformed in a way most viewers show as an
    empty list rather than an error.
    """
    structure_rois = []
    contour_rois = []
    observations = []

    number = 0
    for item in items:
        geometric_type = _CONTOUR_TYPES.get(item.geometry_type)
        if geometric_type is None:
            logger.info(
                "3D item %s is a %s, which RTSTRUCT has no primitive for; omitted",
                item.pk,
                item.geometry_type,
            )
            continue
        points = _contour_points(item)
        if points is None:
            continue
        if geometric_type == "POINT":
            points = points[:1]
        elif len(points) < 3:
            logger.info(
                "3D item %s has %d points, too few for a planar contour; omitted",
                item.pk,
                len(points),
            )
            continue

        number += 1
        name = item.label.display_name if item.label_id else f"ROI {number}"

        roi = Dataset()
        roi.ROINumber = number
        roi.ReferencedFrameOfReferenceUID = series.frame_of_reference_uid
        roi.ROIName = name
        roi.ROIGenerationAlgorithm = "MANUAL"
        structure_rois.append(roi)

        contour = Dataset()
        contour.ContourGeometricType = geometric_type
        contour.NumberOfContourPoints = len(points)
        # DS is a decimal string: six places is under a micron and keeps the element
        # inside its 16-byte-per-value limit for any coordinate a patient can occupy.
        contour.ContourData = [f"{value:.6f}" for value in points.reshape(-1)]
        contour.ContourImageSequence = _contour_images_for(points, source_datasets)

        contour_roi = Dataset()
        contour_roi.ReferencedROINumber = number
        contour_roi.ROIDisplayColor = _display_color(item)
        contour_roi.ContourSequence = [contour]
        contour_rois.append(contour_roi)

        observation = Dataset()
        observation.ObservationNumber = number
        observation.ReferencedROINumber = number
        observation.RTROIInterpretedType = "ORGAN"
        observation.ROIInterpreter = ""
        observations.append(observation)

    return structure_rois, contour_rois, observations


def _contour_images_for(points, source_datasets):
    """The instance a contour sits on, chosen by nearest slice position.

    A planar contour names the image it was drawn on. Yggdrasil stores world
    coordinates and not a slice index, so the instance is found by distance rather than
    assumed -- the nearest ``ImagePositionPatient`` to the contour's centroid. On a
    series whose slices are evenly spaced this is exact; the alternative, omitting the
    reference, produces a structure set some planning systems refuse to load.
    """
    centroid = points.mean(axis=0)
    best = None
    best_distance = None
    for dataset in source_datasets:
        position = getattr(dataset, "ImagePositionPatient", None)
        if position is None:
            continue
        distance = float(
            np.linalg.norm(np.asarray(position, dtype=float) - centroid)
        )
        if best_distance is None or distance < best_distance:
            best, best_distance = dataset, distance
    if best is None:
        return []
    image = Dataset()
    image.ReferencedSOPClassUID = best.SOPClassUID
    image.ReferencedSOPInstanceUID = best.SOPInstanceUID
    return [image]


def _display_color(item):
    """The label's colour as the RGB triplet RTSTRUCT wants, defaulting to red."""
    colour = (getattr(item.label, "color", "") or "").lstrip("#") if item.label_id else ""
    if len(colour) != 6:
        return [255, 0, 0]
    try:
        return [int(colour[index : index + 2], 16) for index in (0, 2, 4)]
    except ValueError:
        return [255, 0, 0]


def build_rtstruct(revision, series, source_datasets):
    """One revision's 3D contours as an RT Structure Set, or ``None`` if it has none."""
    if not (series.frame_of_reference_uid or "").strip():
        raise InteropUnavailable(
            f"DICOM series {series.series_instance_uid} declares no Frame of Reference, "
            "so its contours cannot be expressed as an RT Structure Set"
        )

    items = list(revision.spatialannotation3ditems.select_related("label").all())
    structure_rois, contour_rois, observations = _roi_datasets(
        items, series, source_datasets
    )
    if not structure_rois:
        return None

    reference = source_datasets[0]
    sop_instance_uid = derived_uid(
        "rtstruct-instance", series.series_instance_uid, revision.pk
    )

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = RT_STRUCTURE_SET_STORAGE
    file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    dataset = FileDataset(
        "", {}, file_meta=file_meta, preamble=b"\0" * 128, is_implicit_VR=False
    )
    dataset.SOPClassUID = RT_STRUCTURE_SET_STORAGE
    dataset.SOPInstanceUID = sop_instance_uid
    dataset.Modality = "RTSTRUCT"
    dataset.Manufacturer = "Yggdrasil"
    dataset.ManufacturerModelName = "Yggdrasil annotation export"
    dataset.SoftwareVersions = "3.0"

    # Patient and study identity are copied from the source, which is already
    # pseudonymous: Phase 8 rebuilds every stored instance from a keep-list, so there is
    # nothing here to re-identify and nothing to strip a second time.
    for attribute in (
        "PatientName",
        "PatientID",
        "PatientBirthDate",
        "PatientSex",
        "StudyInstanceUID",
        "StudyDate",
        "StudyTime",
        "AccessionNumber",
        "StudyID",
        "ReferringPhysicianName",
    ):
        setattr(dataset, attribute, getattr(reference, attribute, ""))

    now = datetime.now()
    dataset.SeriesInstanceUID = derived_uid(
        "rtstruct-series", series.series_instance_uid
    )
    dataset.SeriesNumber = 3
    dataset.InstanceNumber = 1
    dataset.StructureSetLabel = "Yggdrasil"
    dataset.StructureSetName = f"Yggdrasil revision {revision.revision_number}"
    dataset.StructureSetDate = now.strftime("%Y%m%d")
    dataset.StructureSetTime = now.strftime("%H%M%S")
    dataset.SeriesDate = dataset.StructureSetDate
    dataset.SeriesTime = dataset.StructureSetTime
    dataset.ContentDate = dataset.StructureSetDate
    dataset.ContentTime = dataset.StructureSetTime

    dataset.ReferencedFrameOfReferenceSequence = _referenced_frame_of_reference(
        series, source_datasets
    )
    dataset.StructureSetROISequence = structure_rois
    dataset.ROIContourSequence = contour_rois
    dataset.RTROIObservationsSequence = observations
    return dataset
