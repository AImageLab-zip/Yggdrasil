"""Which DICOM series an annotation is anchored to, and how to read it back.

The shared half of Phase 9: every writer needs the same two things -- the ordered
source instances of the series an annotation was drawn on, and a stable UID for the
object it is about to produce.

**Resolving the series is not a lookup on one column, and getting that wrong is what
F21 was.** ``common/dicom/ingest`` registers a ``SourceResource`` of kind
``dicom_series`` carrying the ``SeriesInstanceUID``. The *viewer* does not use it: the
volume grid saves through ``annotations.views._measurement_groups``, which always
registers ``ResourceKind.LOGICAL_VOLUME`` against the ``FileRegistry`` row, because the
grid knows a file id and nothing else. So a measurement drawn on a DICOM series is
anchored to a ``logical_volume`` resource whose ``series_instance_uid`` is blank, and
anything asking "is this DICOM?" by reading that column answers no on every real study.

:func:`common.dicom.models.series_for_resource` asks both ways -- the UID when the
resource carries one, otherwise the ``FileRegistry`` row, which is the same row for
both resources because ``register_dicom_series`` deliberately sets ``file``. It lives
in the catalog rather than here because ``annotations.services.sets`` needs the same
answer for the seal (that is the F21 fix), and the durable model must not import an
interchange package to get it.
"""

import io

from pydicom import dcmread

from common.dicom.deidentify import pseudonymous_uid
from common.dicom.models import series_for_resource

__all__ = [
    "InteropUnavailable",
    "derived_uid",
    "instance_datasets",
    "series_for_resource",
]


class InteropUnavailable(Exception):
    """This annotation cannot be expressed in the requested interchange format.

    Raised rather than returned as ``None`` where the *reason* matters -- a series
    whose geometry does not match its labelmap is a real problem someone should see in
    the export log, not an artifact that quietly does not appear.
    """


def instance_datasets(series, *, with_pixels=False):
    """Every instance of a series, in slice order, read back from object storage.

    ``stop_before_pixels`` by default: SEG, SR and RTSTRUCT all reference their sources
    by UID and geometry, and none of them reads a source voxel. A CBCT is several
    hundred instances, so pulling pixel data to write a header reference would move
    gigabytes to produce a few kilobytes.

    Ordering is ``DicomInstance``'s own (``instance_number``, then SOP UID), which is
    what ``common.dicom.models.DicomInstance.Meta.ordering`` already fixes and what the
    viewer's image ids are built in. A SEG's frames are matched to source instances
    positionally, so this order *is* the geometry contract -- it must not be re-derived
    here from ``ImagePositionPatient``, or a series whose numbering disagrees with its
    positions would render one way in the viewer and export another.
    """
    from common.file_access import open_binary

    datasets = []
    for instance in series.instances.all():
        handle, _info = open_binary(instance.object_key)
        try:
            payload = handle.read()
        finally:
            close = getattr(handle, "close", None)
            if close is not None:
                close()
        datasets.append(
            dcmread(io.BytesIO(payload), stop_before_pixels=not with_pixels, force=True)
        )
    if not datasets:
        raise InteropUnavailable(
            f"DICOM series {series.series_instance_uid} has no stored instances"
        )
    return datasets


def derived_uid(purpose, *parts):
    """A stable UID for a generated interop object.

    Derived the way Phase 8 derives every other UID -- ``HMAC(DICOM_UID_HMAC_KEY, ...)``
    under the ISO ``2.25`` arc -- rather than by ``generate_uid()``, so exporting the
    same study twice produces the same document. That is what lets a consumer tell a
    re-export from a genuinely new object, and it makes the export idempotent for free.

    ``purpose`` separates the namespaces: the SEG, the SR and the RTSTRUCT written from
    one revision of one series must not collide, and appending the purpose to the
    HMAC's message is enough to guarantee they do not.
    """
    message = ":".join(["interop", str(purpose), *(str(part) for part in parts)])
    return pseudonymous_uid(message)
