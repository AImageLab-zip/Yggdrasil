"""Strip identity out of a DICOM instance before it is ever written down.

**Whitelist, not blacklist.** The stored dataset is *rebuilt* from an explicit list of
elements rather than copied and pruned. That is the whole design, and it is not
stylistic: a blacklist can never be shown to be complete -- DICOM has thousands of
standard attributes, vendors add private blocks freely, and a tag nobody thought of is
indistinguishable from a tag nobody has yet. A whitelist inverts the failure: an
unknown element is dropped by default, so the mistake is a missing feature rather than
a leaked name, and :func:`assert_no_phi` can be an actual assertion instead of a spot
check.

**Header only, and it says so.** Nothing here reads pixels. Text burned into the image
is invisible to this module, which is why an instance that admits to carrying it, or
that belongs to a Secondary Capture class where it is the norm, is *refused* rather
than accepted and flagged (roadmap risk 9). ``deid_confidence`` records what was
actually established, and no caller may report more than it says.

**UIDs are derived, not mapped.** ``pseudonymous_uid`` is
``HMAC(key, original)`` rendered under the ISO ``2.25`` arc, so the same study
re-ingested twice lands on the same identifiers with no lookup table in between. The
roadmap planned a ``DicomUidMap`` and its own risk register calls that table "a
re-identification vector ... safely droppable"; a table that is safely droppable is not
load-bearing, and one that does not exist cannot leak. Rotating
``DICOM_UID_HMAC_KEY`` renames every series, which is a migration, not a maintenance
task -- see docs/setup.md.
"""

import hashlib
import hmac

from django.conf import settings
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.tag import Tag
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

#: Secondary Capture: an image *of* something else -- a screenshot of a report, a
#: scanned film -- where burned-in identifiers are the norm rather than the exception.
#: Header-only de-identification cannot make one of these safe, so they are refused.
SECONDARY_CAPTURE_SOP_CLASSES = frozenset(
    {
        "1.2.840.10008.5.1.4.1.1.7",  # Secondary Capture Image Storage
        "1.2.840.10008.5.1.4.1.1.7.1",  # Multi-frame Single Bit SC
        "1.2.840.10008.5.1.4.1.1.7.2",  # Multi-frame Grayscale Byte SC
        "1.2.840.10008.5.1.4.1.1.7.3",  # Multi-frame Grayscale Word SC
        "1.2.840.10008.5.1.4.1.1.7.4",  # Multi-frame True Colour SC
    }
)

#: Copied through verbatim. Every entry is here because something downstream reads it:
#: the volume geometry, the modality LUT, the VOI defaults, the pixel decoder, or the
#: slice ordering. Nothing descriptive, nothing about a person, nothing about a visit.
#:
#: Adding a keyword here is a deliberate act with a test; that is the point of the list
#: being short enough to read in one screen.
KEPT_KEYWORDS = frozenset(
    {
        # --- identity of the image data itself (rewritten to pseudonyms below) ------
        "SOPClassUID",
        "SOPInstanceUID",
        "SeriesInstanceUID",
        "StudyInstanceUID",
        "FrameOfReferenceUID",
        # --- what kind of image this is --------------------------------------------
        "Modality",
        "ImageType",
        # --- ordering ---------------------------------------------------------------
        "InstanceNumber",
        "SeriesNumber",
        # --- geometry: without these the volume cannot be built ---------------------
        "ImagePositionPatient",
        "ImageOrientationPatient",
        "PixelSpacing",
        "SliceThickness",
        "SpacingBetweenSlices",
        "PatientPosition",
        # --- pixel decoding ---------------------------------------------------------
        "Rows",
        "Columns",
        "NumberOfFrames",
        "SamplesPerPixel",
        "PhotometricInterpretation",
        "PlanarConfiguration",
        "BitsAllocated",
        "BitsStored",
        "HighBit",
        "PixelRepresentation",
        "SmallestImagePixelValue",
        "LargestImagePixelValue",
        # --- real-world values: decision #5 is unimplementable without these --------
        "RescaleSlope",
        "RescaleIntercept",
        "RescaleType",
        "WindowCenter",
        "WindowWidth",
        "WindowCenterWidthExplanation",
        "VOILUTFunction",
        # --- the bytes --------------------------------------------------------------
        "PixelData",
    }
)

#: Type-2 elements an IOD requires to be *present*, which therefore cannot simply be
#: dropped: a reader that demands them would reject the file. They are emitted with
#: fixed, non-identifying values instead -- the PS3.15 "replace with a dummy" rule.
#: ``PatientID`` carries Yggdrasil's own patient identifier, which is already the
#: pseudonym this platform works in.
BLANKED_KEYWORDS = (
    "PatientName",
    "PatientBirthDate",
    "PatientSex",
    "StudyDate",
    "StudyTime",
    "SeriesDate",
    "SeriesTime",
    "AcquisitionDate",
    "AcquisitionTime",
    "ContentDate",
    "ContentTime",
    "AccessionNumber",
    "ReferringPhysicianName",
    "StudyID",
)

#: The value written into every blanked element above, except ``PatientID``.
ANONYMOUS_NAME = "ANONYMOUS"

#: What ``deid_confidence`` may say. Never more than was actually established.
CONFIDENCE_DECLARED_CLEAN = "declared_clean"  # BurnedInAnnotation == 'NO'
CONFIDENCE_HEADER_ONLY = "header_only"  # the tag was absent; pixels unread

#: The ISO/IEC 9834-8 arc for UIDs derived from a value rather than registered
#: (DICOM PS3.5 B.2). Using it means Yggdrasil needs no assigned OID root.
UID_ROOT = "2.25"


class DicomRefused(ValueError):
    """The instance cannot be stored safely, and no de-identified form of it can be.

    Distinct from a parse failure: the file was understood, and understanding it is
    what produced the refusal.
    """


class PhiLeak(AssertionError):
    """A written dataset carried an element the whitelist does not allow.

    ``AssertionError`` deliberately: this is an invariant violation, not a user error,
    and the only correct response is to abort the transaction that produced it.
    """


def _hmac_key():
    """The key pseudonymous UIDs are derived under.

    Falls back to ``SECRET_KEY`` rather than refusing, because a deployment that has
    not set the dedicated key still needs deterministic, non-reversible UIDs -- and a
    hard failure here would take the whole upload path down for a value that has a
    sane default. Documented in docs/setup.md as worth setting separately, since
    rotating ``SECRET_KEY`` would otherwise rename every stored series.
    """
    key = getattr(settings, "DICOM_UID_HMAC_KEY", "") or settings.SECRET_KEY
    return key.encode("utf-8") if isinstance(key, str) else bytes(key)


def pseudonymous_uid(original_uid):
    """A stable, non-reversible replacement for one DICOM UID.

    Deterministic, so re-ingesting a study is idempotent and a series keeps its name
    across uploads. Non-reversible, so the stored value tells an attacker with the
    database nothing about the originating institution's numbering.

    :param original_uid: the UID as it arrived. Required; a blank one is a malformed
        instance rather than something to invent a name for.
    :returns: a valid DICOM UID under :data:`UID_ROOT`, at most 44 characters.
    """
    text = str(original_uid or "").strip()
    if not text:
        raise DicomRefused("a required UID is missing")
    digest = hmac.new(_hmac_key(), text.encode("utf-8"), hashlib.sha256).digest()
    # 128 bits -> at most 39 decimal digits; '2.25.' + 39 = 44, inside the 64-char
    # limit for a UID with room to spare.
    return f"{UID_ROOT}.{int.from_bytes(digest[:16], 'big')}"


def refusal_reason(dataset):
    """Why this instance may not be stored, or ``None`` if it may.

    Separate from :func:`deidentify` so the ingest can refuse a whole upload on a
    header pass, before writing anything, and report every reason at once.
    """
    sop_class = str(getattr(dataset, "SOPClassUID", "") or "")
    if sop_class in SECONDARY_CAPTURE_SOP_CLASSES:
        return (
            "Secondary Capture images (screenshots, scanned film) routinely carry "
            "identifiers burned into the pixels, which header de-identification "
            "cannot remove. This file was not stored."
        )
    burned_in = str(getattr(dataset, "BurnedInAnnotation", "") or "").strip().upper()
    if burned_in == "YES":
        return (
            "This image declares burned-in annotation, which may include patient "
            "identifiers in the pixels. Header de-identification cannot remove it, "
            "so the file was not stored."
        )
    return None


def confidence_for(dataset):
    """How much the header actually established about burned-in identifiers.

    Two values, and the weaker one is the common case. Nothing may present
    ``header_only`` to a user as "de-identified"; it means "no identifiers in the
    header, and the pixels were not examined".
    """
    burned_in = str(getattr(dataset, "BurnedInAnnotation", "") or "").strip().upper()
    return CONFIDENCE_DECLARED_CLEAN if burned_in == "NO" else CONFIDENCE_HEADER_ONLY


def deidentify(dataset, *, patient_id):
    """Build the dataset that will be stored, from scratch.

    :param dataset: the instance as read, pixels included.
    :param patient_id: Yggdrasil's own identifier for the patient, written into
        ``PatientID`` so the stored file is self-describing within this platform and
        outside nobody's.
    :returns: a new :class:`~pydicom.dataset.FileDataset`, ready to write.
    :raises DicomRefused: if the instance may not be stored at all.
    """
    reason = refusal_reason(dataset)
    if reason:
        raise DicomRefused(reason)

    clean = Dataset()
    for keyword in KEPT_KEYWORDS:
        if keyword in dataset:
            clean[keyword] = dataset[keyword]

    # The four UIDs that name this data are replaced, not kept: the originals encode
    # the sending institution's numbering and are a re-identification handle on their
    # own. SOPClassUID is *not* among them -- it names a standard, not an instance.
    for keyword in (
        "SOPInstanceUID",
        "SeriesInstanceUID",
        "StudyInstanceUID",
        "FrameOfReferenceUID",
    ):
        if keyword in dataset:
            clean[keyword].value = pseudonymous_uid(dataset[keyword].value)

    for keyword in BLANKED_KEYWORDS:
        setattr(clean, keyword, "")
    clean.PatientName = ANONYMOUS_NAME
    clean.PatientID = str(patient_id)
    clean.PatientIdentityRemoved = "YES"
    clean.DeidentificationMethod = "Yggdrasil header whitelist"

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = clean.get("SOPClassUID", "")
    meta.MediaStorageSOPInstanceUID = clean.get("SOPInstanceUID", "")
    # **Carried over, not chosen.** ``PixelData`` is copied byte for byte and never
    # re-encoded -- a header-only pass has no business rewriting pixels -- so the
    # stored bytes are only interpretable under the syntax they arrived in. Forcing
    # Explicit VR Little Endian here would silently mislabel every compressed
    # instance, and JPEG Lossless is ordinary for CBCT rather than exotic.
    incoming_meta = getattr(dataset, "file_meta", None)
    meta.TransferSyntaxUID = (
        getattr(incoming_meta, "TransferSyntaxUID", None) or ExplicitVRLittleEndian
    )
    meta.ImplementationClassUID = generate_uid()

    # Encoding is carried by ``meta.TransferSyntaxUID`` alone. The ``is_little_endian``
    # / ``is_implicit_VR`` attributes say the same thing a second time and are removed
    # in pydicom 4.0, so setting them would be both redundant and a deprecation warning
    # on every instance ingested.
    return FileDataset(None, clean, file_meta=meta, preamble=b"\0" * 128)


#: Elements the whitelist produces that are not in :data:`KEPT_KEYWORDS`: the blanked
#: Type-2 set plus the three this module adds to say what it did.
_EMITTED_KEYWORDS = (
    KEPT_KEYWORDS
    | set(BLANKED_KEYWORDS)
    | {"PatientID", "PatientIdentityRemoved", "DeidentificationMethod"}
)


def assert_no_phi(dataset):
    """Refuse a dataset carrying anything the whitelist does not emit.

    Run against what was actually written, not against what was meant to be: the
    check is only worth having if it reads back the bytes. Any element outside
    :data:`KEPT_KEYWORDS` and the blanked set -- a private block, a vendor extension,
    an attribute added by a future edit to :func:`deidentify` that nobody reviewed --
    raises, and the caller's transaction is expected to abort.

    :raises PhiLeak: naming every offending element, so one run reports them all.
    """
    offenders = []
    for element in dataset:
        tag = Tag(element.tag)
        if tag.is_private:
            offenders.append(f"private {tag}")
            continue
        keyword = element.keyword or str(tag)
        if keyword not in _EMITTED_KEYWORDS:
            offenders.append(keyword)
    if offenders:
        raise PhiLeak(
            "de-identified dataset carries "
            f"{len(offenders)} element(s) outside the whitelist: "
            + ", ".join(sorted(offenders))
        )
    return dataset
