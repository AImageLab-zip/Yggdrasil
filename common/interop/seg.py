"""A dense labelmap as DICOM SEG.

The source is the CBCT pipeline's ``segmentation_nifti`` -- the only dense segmentation
the platform produces today -- rendered against the DICOM series it was computed from.

**The geometry is not resampled here, and that is deliberate.** Since Phase 8 the runner
is handed the series prefix and reads DICOM directly, so its output labelmap is already
on the series' own grid; a resample would be re-gridding data that is already aligned and
would introduce interpolation error to fix nothing. What this module does instead is
*check* the alignment and refuse when it does not hold. A labelmap whose shape disagrees
with the series is either from a different study or from a pipeline that silently
re-gridded, and writing a SEG from it would produce an object that looks authoritative
and is misaligned by an unknown amount. ``highdicom`` would not catch it: it matches
frames to source images positionally and asks no questions.

**Frame order is the source-instance order, not the NIfTI's.** ``instance_datasets``
returns instances in ``DicomInstance`` order, which is the order the viewer builds its
image ids in. NIfTI arrays are indexed in their own axis order, which for an
LPS-acquired CBCT read through ``nibabel`` is usually the reverse along the slice axis.
Rather than guess, the slice axis is oriented by comparing the first and last
``ImagePositionPatient`` against the affine, so a flipped export is a failed assertion
rather than a segmentation drawn on the wrong end of the jaw.
"""

import logging

import numpy as np
from pydicom.sr.coding import Code

from common.interop.sources import InteropUnavailable, derived_uid

logger = logging.getLogger(__name__)

#: What a segment *is*, when the record does not say. The CBCT pipeline segments
#: anatomical structure; there is no more specific standard term that is true of every
#: label it produces, and inventing one per label would be a claim the record does not
#: make.
_DEFAULT_CATEGORY = Code("123037004", "SCT", "Anatomical Structure")
_DEFAULT_TYPE = Code("91723000", "SCT", "Anatomical structure")


def _oriented_slices(volume, source_datasets, affine):
    """The labelmap as ``(frames, rows, columns)`` matching ``source_datasets`` order.

    The slice axis is the one the affine moves along, and its direction is settled by
    the *stored* positions rather than by convention: the vector from the first
    instance's ``ImagePositionPatient`` to the last is compared with the affine's slice
    direction, and the array is flipped when they disagree. That is one comparison
    against data both sides already carry, instead of a rule about NIfTI conventions
    that is right for most files and silently wrong for the rest.
    """
    if volume.ndim != 3:
        raise InteropUnavailable(
            f"a segmentation labelmap must be 3D; this one is {volume.ndim}D"
        )
    frames = len(source_datasets)
    rows = int(source_datasets[0].Rows)
    columns = int(source_datasets[0].Columns)

    # nibabel gives (columns, rows, slices) for a standard LPS/RAS volume.
    if volume.shape != (columns, rows, frames):
        raise InteropUnavailable(
            f"labelmap shape {volume.shape} does not match the series' "
            f"{(columns, rows, frames)} (columns, rows, instances); the two are not "
            "the same grid, so a SEG written from them would be misaligned"
        )
    slices = np.transpose(volume, (2, 1, 0))

    first = np.asarray(source_datasets[0].ImagePositionPatient, dtype=float)
    last = np.asarray(source_datasets[-1].ImagePositionPatient, dtype=float)
    if frames > 1:
        stored_direction = last - first
        affine_direction = np.asarray(affine, dtype=float)[:3, 2]
        if float(np.dot(stored_direction, affine_direction)) < 0:
            slices = slices[::-1]
    return np.ascontiguousarray(slices)


def _segment_descriptions(labels, present_values):
    """One description per label value actually present in the labelmap.

    Segment numbers must be 1..N and contiguous, which is *not* the same as the stored
    label values -- an FDI code is 11..48 and a labelmap may use any subset. The
    original value is kept as the segment's tracking id so a consumer can map back,
    rather than being lost in the renumbering.
    """
    import highdicom as hd

    descriptions = []
    for number, value in enumerate(sorted(present_values), start=1):
        label = labels.get(value)
        descriptions.append(
            hd.seg.SegmentDescription(
                segment_number=number,
                segment_label=(label.display_name if label else f"Label {value}"),
                segmented_property_category=_DEFAULT_CATEGORY,
                segmented_property_type=_DEFAULT_TYPE,
                algorithm_type=hd.seg.SegmentAlgorithmTypeValues.AUTOMATIC,
                algorithm_identification=hd.AlgorithmIdentificationSequence(
                    name="Yggdrasil CBCT segmentation",
                    version="1",
                    family=Code("123109", "DCM", "Segmentation"),
                ),
                tracking_id=(label.code if label else f"label-{value}"),
                tracking_uid=derived_uid("seg-segment", value),
            )
        )
    return descriptions


def build_seg(volume, affine, series, source_datasets, *, labels=None, revision_key=""):
    """A labelmap volume as a DICOM SEG, or ``None`` when it is empty.

    :param volume: the labelmap as read by ``nibabel``, shape (columns, rows, slices).
    :param affine: that volume's affine, used only to settle slice direction.
    :param series: the ``DicomSeries`` the labelmap was computed on.
    :param source_datasets: that series' instances, in order.
    :param labels: ``{stored value: LabelDefinition}``, for segment naming.
    :param revision_key: makes the derived SOP UID unique per revision.
    """
    import highdicom as hd

    slices = _oriented_slices(volume, source_datasets, affine)
    present = sorted(int(v) for v in np.unique(slices) if int(v) != 0)
    if not present:
        return None

    # highdicom takes segments as a 4th axis of 0/1 masks. Building it explicitly
    # rather than passing the label array keeps the 1..N renumbering visible here,
    # where the tracking ids that undo it are also written.
    stacked = np.stack(
        [(slices == value).astype(np.uint8) for value in present], axis=-1
    )
    return hd.seg.Segmentation(
        source_images=source_datasets,
        pixel_array=stacked,
        segmentation_type=hd.seg.SegmentationTypeValues.BINARY,
        segment_descriptions=_segment_descriptions(labels or {}, present),
        series_instance_uid=derived_uid("seg-series", series.series_instance_uid),
        series_number=2,
        sop_instance_uid=derived_uid(
            "seg-instance", series.series_instance_uid, revision_key
        ),
        instance_number=1,
        manufacturer="Yggdrasil",
        manufacturer_model_name="Yggdrasil annotation export",
        software_versions="3.0",
        device_serial_number="0",
        content_description="Yggdrasil segmentation export",
        omit_empty_frames=False,
    )
