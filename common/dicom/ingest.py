"""Store an uploaded DICOM folder as DICOM, and catalog what was stored.

The point of the whole phase in one module. Before it, a DICOM series reaching the
upload page was converted to a single ``.nii.gz`` in the browser and the original was
discarded -- there was no server-side reader, and ``maxillo.file_utils`` carried a
function whose only job was to say so.

Three properties this is built to hold:

* **Nothing is written before everything is checked.** A header-only pass reads every
  file and collects every refusal first, so an upload containing one Secondary Capture
  image is rejected whole with a reason, rather than half-stored and then abandoned.
* **De-identification is not optional and not partial.** Every instance goes through
  :func:`~common.dicom.deidentify.deidentify`, and the exact byte buffer handed to
  object storage is what :func:`~common.dicom.deidentify.assert_no_phi` is run
  against. A leak aborts the transaction rather than logging.
* **A series is one ``FileRegistry`` row with a prefix path**, in the shape
  ``maxillo.file_utils.save_generic_modality_folder`` already writes for folder
  uploads, so every consumer that handles a prefix row handles a series unchanged.

Domain-agnostic on purpose: the caller passes the ``file_type`` and modality it already
knows, so this module never imports a domain app and brain MRI can use it without a
line of new code here.
"""

import hashlib
import io
import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone
from pydicom import dcmread
from pydicom.errors import InvalidDicomError

from common.dicom.deidentify import (
    DicomRefused,
    assert_no_phi,
    confidence_for,
    deidentify,
    pseudonymous_uid,
    refusal_reason,
)
from annotations.services import register_dicom_series
from common.dicom.models import DicomInstance, DicomSeries
from common.object_storage import get_object_storage
from common.uploads import entity_fk_kwargs, raw_key_prefix_for

logger = logging.getLogger(__name__)

#: Names this de-identification by, recorded on every series so a later change to the
#: whitelist is distinguishable from data that predates it.
DEID_PROFILE = "whitelist-v1"

#: A DICOMDIR is an index of a media set, not an image, and carries the patient
#: directory records this phase exists to avoid storing. Skipped silently: it is
#: normal for a burned disc to contain one, and it is not an error for the uploader.
MEDIA_STORAGE_DIRECTORY = "1.2.840.10008.1.3.10"


class DicomIngestError(ValueError):
    """The upload was not stored, and why. Every reason at once.

    One exception for the whole upload rather than one per file: a study is ingested
    whole or not at all, so a caller has nothing useful to do with the first reason in
    isolation.
    """

    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__(" ".join(self.reasons))


@dataclass
class _PendingInstance:
    """One instance that passed the header pass and is queued to be written."""

    upload: object
    sop_instance_uid: str
    series_instance_uid: str
    study_instance_uid: str
    instance_number: int


@dataclass
class _PendingSeries:
    """One series' worth of :class:`_PendingInstance`, plus what the header pass saw."""

    series_instance_uid: str
    study_instance_uid: str
    frame_of_reference_uid: str = ""
    dicom_modality: str = ""
    sop_class_uid: str = ""
    transfer_syntax_uid: str = ""
    rows: int = 0
    columns: int = 0
    deid_confidence: str = ""
    instances: list = field(default_factory=list)


def _read_header(upload):
    """Read one uploaded file's header, or ``None`` if it is not DICOM.

    ``stop_before_pixels`` because the header pass runs over every file in the folder
    and a 500 MB study should not be decoded twice to find out it will be refused.
    """
    upload.seek(0)
    try:
        return dcmread(upload, stop_before_pixels=True)
    except (InvalidDicomError, AttributeError, ValueError, EOFError):
        return None
    finally:
        upload.seek(0)


def _plan(files):
    """Header pass: group into series, or raise with every reason the upload failed.

    Returns ``{series_uid: _PendingSeries}`` keyed by the *original* series UID; the
    pseudonyms are derived at write time, so a failure costs nothing.
    """
    reasons = []
    non_dicom = []
    plan = {}

    for upload in files:
        name = getattr(upload, "name", "file")
        header = _read_header(upload)
        if header is None:
            non_dicom.append(name)
            continue
        if str(getattr(header, "SOPClassUID", "")) == MEDIA_STORAGE_DIRECTORY:
            continue  # a DICOMDIR index, not an image

        refusal = refusal_reason(header)
        if refusal:
            reasons.append(f"{name}: {refusal}")
            continue

        series_uid = str(getattr(header, "SeriesInstanceUID", "") or "")
        sop_uid = str(getattr(header, "SOPInstanceUID", "") or "")
        if not series_uid or not sop_uid:
            reasons.append(
                f"{name}: the file is missing SeriesInstanceUID or SOPInstanceUID, "
                "so it cannot be filed against a series."
            )
            continue

        pending = plan.get(series_uid)
        if pending is None:
            file_meta = getattr(header, "file_meta", None)
            pending = _PendingSeries(
                series_instance_uid=series_uid,
                study_instance_uid=str(getattr(header, "StudyInstanceUID", "") or ""),
                frame_of_reference_uid=str(
                    getattr(header, "FrameOfReferenceUID", "") or ""
                ),
                dicom_modality=str(getattr(header, "Modality", "") or ""),
                sop_class_uid=str(getattr(header, "SOPClassUID", "") or ""),
                transfer_syntax_uid=str(
                    getattr(file_meta, "TransferSyntaxUID", "") or ""
                ),
                rows=int(getattr(header, "Rows", 0) or 0),
                columns=int(getattr(header, "Columns", 0) or 0),
                deid_confidence=confidence_for(header),
            )
            plan[series_uid] = pending

        pending.instances.append(
            _PendingInstance(
                upload=upload,
                sop_instance_uid=sop_uid,
                series_instance_uid=series_uid,
                study_instance_uid=pending.study_instance_uid,
                instance_number=int(getattr(header, "InstanceNumber", 0) or 0),
            )
        )

    if reasons:
        raise DicomIngestError(reasons)
    if not plan:
        raise DicomIngestError(
            [
                "No DICOM images were found in the upload"
                + (f" ({len(non_dicom)} file(s) were not DICOM)." if non_dicom else "."),
            ]
        )
    if non_dicom:
        logger.info("DICOM ingest ignored %d non-DICOM file(s)", len(non_dicom))
    return plan


def _encode(upload, *, patient_id):
    """De-identify one instance and return the exact bytes that will be stored.

    The buffer returned here is what goes to object storage *and* what
    :func:`assert_no_phi` is run against, re-read from those bytes. Asserting against
    the in-memory dataset instead would be checking the intention rather than the
    artifact, which is the difference between a test and a guarantee.
    """
    upload.seek(0)
    dataset = dcmread(upload)
    clean = deidentify(dataset, patient_id=patient_id)

    buffer = io.BytesIO()
    clean.save_as(buffer, enforce_file_format=False)
    written = buffer.getvalue()

    assert_no_phi(dcmread(io.BytesIO(written), force=True))
    return clean, written


@transaction.atomic
def ingest_dicom_series(patient, *, modality_slug, file_type, files, modality=None):
    """Store an uploaded DICOM folder natively and catalog it.

    :param patient: the domain ``Patient`` the series belongs to.
    :param modality_slug: Yggdrasil's modality slug (``'cbct'``), used for the storage
        prefix and the Job routing key -- *not* the DICOM ``Modality`` tag.
    :param file_type: the ``FileRegistry.file_type`` for a raw input of this modality.
        Passed in rather than derived, so this module never imports a domain app.
    :param files: uploaded file objects, in any order, possibly spanning several series.
    :param modality: the ``common.Modality`` row, when one exists.
    :returns: the created :class:`~common.dicom.models.DicomSeries` list, in the order
        the series were first seen.
    :raises DicomIngestError: with every reason, having written nothing.
    """
    from common.modality_config import get_step
    from common.models import FileRegistry

    # Risk 10, the other half: an admin may have set the flag before any series
    # existed. Storing one under it would put the bytes in object storage and leave
    # the viewer empty, which reads as a broken upload rather than a policy.
    step = get_step(modality_slug)
    if step is not None and step.discard_raw:
        raise DicomIngestError(
            [
                f"The '{modality_slug}' modality is configured to discard raw inputs. "
                "A DICOM series is the volume the viewer displays, so storing one "
                "under that setting would leave this patient with an empty viewer. "
                "Clear 'discard raw' on the processing step first."
            ]
        )

    plan = _plan(files)
    storage = get_object_storage()
    patient_id = getattr(patient, "patient_id", None) or getattr(patient, "pk", "")
    base_prefix = (
        f"{raw_key_prefix_for(patient, modality_slug)}/"
        f"{modality_slug}_patient_{patient_id}_dicom"
    )

    created = []
    for pending in plan.values():
        series_uid = pseudonymous_uid(pending.series_instance_uid)
        study_uid = pseudonymous_uid(pending.study_instance_uid or pending.series_instance_uid)

        existing = DicomSeries.objects.filter(series_instance_uid=series_uid).first()
        if existing is not None:
            # Pseudonymous UIDs are derived, so the same series always lands on the
            # same name -- which makes a duplicate upload detectable instead of
            # silently forking the study into two rows nothing relates.
            raise DicomIngestError(
                [
                    f"This series is already stored (as file {existing.file_id}). "
                    "Re-uploading it would create a second copy that no annotation, "
                    "export or job could tell apart from the first."
                ]
            )

        prefix = f"{base_prefix}/{series_uid}"
        members, total_size, written_instances = [], 0, []

        for item in sorted(pending.instances, key=lambda i: (i.instance_number, i.sop_instance_uid)):
            clean, payload = _encode(item.upload, patient_id=patient_id)
            sop_uid = str(clean.SOPInstanceUID)
            key = f"{prefix}/{sop_uid}.dcm"
            storage.upload_fileobj(
                io.BytesIO(payload), key=key, content_type="application/dicom"
            )
            digest = hashlib.sha256(payload).hexdigest()
            total_size += len(payload)
            members.append(
                {
                    "name": f"{sop_uid}.dcm",
                    "path": key,
                    "size": len(payload),
                    "hash": digest,
                }
            )
            written_instances.append(
                DicomInstance(
                    sop_instance_uid=sop_uid,
                    instance_number=item.instance_number,
                    object_key=key,
                    file_size=len(payload),
                    content_hash=digest,
                    frame_count=int(getattr(clean, "NumberOfFrames", 1) or 1),
                    image_position_patient=[
                        float(v) for v in (getattr(clean, "ImagePositionPatient", []) or [])
                    ],
                    image_orientation_patient=[
                        float(v)
                        for v in (getattr(clean, "ImageOrientationPatient", []) or [])
                    ],
                )
            )

        # The same folder hash the existing folder-upload path computes: a digest over
        # the members' digests, so the row has a stable identity without pretending a
        # prefix has bytes of its own.
        folder_hash = hashlib.sha256(
            "".join(member["hash"] for member in members).encode()
        ).hexdigest()

        registry_row = FileRegistry.objects.create(
            file_type=file_type,
            file_path=prefix,
            file_size=total_size,
            file_hash=folder_hash,
            modality=modality,
            **entity_fk_kwargs(patient),
            metadata={
                "uploaded_at": timezone.now().isoformat(),
                "input_type": "dicom_series",
                "file_format": "dicom",
                "file_count": len(members),
                "modality_slug": modality_slug,
                "series_instance_uid": series_uid,
                "study_instance_uid": study_uid,
                "files": members,
            },
        )

        series = DicomSeries.objects.create(
            file=registry_row,
            series_instance_uid=series_uid,
            study_instance_uid=study_uid,
            frame_of_reference_uid=(
                pseudonymous_uid(pending.frame_of_reference_uid)
                if pending.frame_of_reference_uid
                else ""
            ),
            dicom_modality=pending.dicom_modality,
            sop_class_uid=pending.sop_class_uid,
            transfer_syntax_uid=pending.transfer_syntax_uid,
            instance_count=len(written_instances),
            rows=pending.rows,
            columns=pending.columns,
            deid_profile=DEID_PROFILE,
            deid_confidence=pending.deid_confidence,
        )
        for instance in written_instances:
            instance.series = series
        # bulk_create bypasses ``DicomInstance.save()`` and therefore the seal check.
        # Sound here and only here: the series was created three lines ago, so it
        # cannot be sealed. Nowhere else may write instances in bulk.
        DicomInstance.objects.bulk_create(written_instances)

        # Anchor it for annotations now rather than on first use. A resource created
        # lazily by whichever surface happens to annotate first is a resource whose
        # descriptor depends on who got there first; created here it always records
        # the grid as ingested.
        register_dicom_series(
            series,
            descriptor={
                "rows": series.rows,
                "columns": series.columns,
                "instance_count": series.instance_count,
                "modality": series.dicom_modality,
            },
        )

        created.append(series)

    return created
