"""De-identification: the property the whole DICOM phase rests on.

The sentinel test below is the reason this file exists. Everything else in Phase 8 --
the catalog, the serving endpoints, the viewer -- is recoverable if it is wrong. A
patient name written into object storage is not.
"""

import hashlib

from django.test import TestCase, override_settings
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

from common.dicom.deidentify import (
    ANONYMOUS_NAME,
    CONFIDENCE_DECLARED_CLEAN,
    CONFIDENCE_HEADER_ONLY,
    KEPT_KEYWORDS,
    UID_ROOT,
    DicomRefused,
    PhiLeak,
    assert_no_phi,
    confidence_for,
    deidentify,
    pseudonymous_uid,
    refusal_reason,
)

#: Values that must not survive de-identification, one per attribute that carries
#: them. Searched for by *value* in the output, so a tag copied under a different
#: keyword is caught too.
PHI_MARKERS = {
    "PatientName": "SENTINEL^PATIENT",
    "PatientBirthDate": "19510203",
    "PatientAddress": "12 Sentinel Street, Modena",
    "PatientTelephoneNumbers": "+390590000000",
    "OtherPatientIDs": "SENTINEL-OTHER-ID",
    "ReferringPhysicianName": "SENTINEL^REFERRER",
    "PerformingPhysicianName": "SENTINEL^PERFORMER",
    "OperatorsName": "SENTINEL^OPERATOR",
    "InstitutionName": "Sentinel Hospital",
    "InstitutionAddress": "99 Sentinel Road",
    "StationName": "SENTINELSTATION",
    "AccessionNumber": "SENTINELACC",
    "StudyID": "SENTINELSTUDY",
    "StudyDescription": "Sentinel study description",
    "SeriesDescription": "Sentinel series description",
    "RequestingPhysician": "SENTINEL^REQUESTER",
    "MedicalRecordLocator": "SENTINEL-MRN",
    "PatientComments": "Sentinel free-text comment",
    "AdditionalPatientHistory": "Sentinel history",
    "DeviceSerialNumber": "SENTINEL-SERIAL",
}


def synthetic_instance(
    *,
    sop_instance_uid="1.2.826.0.1.3680043.9.7.1.1",
    series_instance_uid="1.2.826.0.1.3680043.9.7.1",
    study_instance_uid="1.2.826.0.1.3680043.9.7",
    instance_number=1,
    position=(0.0, 0.0, 0.0),
    with_phi=False,
    burned_in=None,
    sop_class_uid=CTImageStorage,
    rows=4,
    columns=4,
):
    """A minimal but conformant CT instance, built in memory.

    Synthesised rather than committed as a binary fixture: a generated dataset carries
    no real patient by construction, is diffable, and cannot quietly become the only
    record of a study nobody may distribute.
    """
    ds = Dataset()
    ds.SOPClassUID = sop_class_uid
    ds.SOPInstanceUID = sop_instance_uid
    ds.SeriesInstanceUID = series_instance_uid
    ds.StudyInstanceUID = study_instance_uid
    ds.FrameOfReferenceUID = "1.2.826.0.1.3680043.9.7.99"
    ds.Modality = "CT"
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.InstanceNumber = instance_number
    ds.SeriesNumber = 1
    ds.ImagePositionPatient = [float(v) for v in position]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.PixelSpacing = [0.25, 0.25]
    ds.SliceThickness = 0.25
    ds.PatientPosition = "HFS"
    ds.Rows = rows
    ds.Columns = columns
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 1
    ds.RescaleSlope = 1
    ds.RescaleIntercept = -1024
    ds.WindowCenter = 300
    ds.WindowWidth = 2000
    ds.PixelData = bytes(rows * columns * 2)
    ds.PatientID = "ORIGINAL-ID"

    if burned_in is not None:
        ds.BurnedInAnnotation = burned_in
    if with_phi:
        for keyword, value in PHI_MARKERS.items():
            setattr(ds, keyword, value)
        # A vendor private block, which is where the attributes nobody enumerated live.
        block = ds.private_block(0x0099, "SENTINEL VENDOR", create=True)
        block.add_new(0x01, "LO", "SENTINEL-PRIVATE")

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = ds.SOPClassUID
    meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = meta
    return ds


class DeidentificationSentinelTests(TestCase):
    """Nothing identifying survives, and the check that says so is real."""

    def test_no_phi_marker_survives_deidentification(self):
        ds = synthetic_instance(with_phi=True)
        clean = deidentify(ds, patient_id="P-42")

        # By value, not by tag: a name copied into a differently-keyworded element
        # would pass a tag-by-tag check and fail this one.
        rendered = str(clean)
        for keyword, marker in PHI_MARKERS.items():
            self.assertNotIn(marker, rendered, f"{keyword} survived de-identification")
        self.assertNotIn("SENTINEL-PRIVATE", rendered)
        self.assertNotIn("ORIGINAL-ID", rendered)

    def test_written_bytes_pass_assert_no_phi(self):
        clean = deidentify(synthetic_instance(with_phi=True), patient_id="P-42")
        # No exception is the assertion.
        assert_no_phi(clean)

    def test_assert_no_phi_catches_an_element_the_whitelist_does_not_emit(self):
        clean = deidentify(synthetic_instance(), patient_id="P-42")
        clean.InstitutionName = "Somewhere"
        with self.assertRaises(PhiLeak) as caught:
            assert_no_phi(clean)
        self.assertIn("InstitutionName", str(caught.exception))

    def test_assert_no_phi_catches_a_private_block(self):
        clean = deidentify(synthetic_instance(), patient_id="P-42")
        block = clean.private_block(0x0099, "LATE ADDITION", create=True)
        block.add_new(0x01, "LO", "anything")
        with self.assertRaises(PhiLeak):
            assert_no_phi(clean)

    def test_every_offender_is_reported_in_one_run(self):
        clean = deidentify(synthetic_instance(), patient_id="P-42")
        clean.InstitutionName = "Somewhere"
        clean.StationName = "SOMESTATION"
        with self.assertRaises(PhiLeak) as caught:
            assert_no_phi(clean)
        self.assertIn("InstitutionName", str(caught.exception))
        self.assertIn("StationName", str(caught.exception))


class DeidentificationOutputTests(TestCase):
    """What survives, and what it is replaced with."""

    def test_geometry_and_pixel_attributes_are_preserved_exactly(self):
        ds = synthetic_instance(with_phi=True, position=(1.5, -2.5, 30.0))
        clean = deidentify(ds, patient_id="P-42")

        self.assertEqual(list(clean.ImagePositionPatient), [1.5, -2.5, 30.0])
        self.assertEqual(list(clean.ImageOrientationPatient), [1, 0, 0, 0, 1, 0])
        self.assertEqual(list(clean.PixelSpacing), [0.25, 0.25])
        self.assertEqual(clean.Rows, 4)
        self.assertEqual(clean.Columns, 4)
        self.assertEqual(clean.PixelData, ds.PixelData)

    def test_the_modality_lut_survives_because_decision_5_needs_it(self):
        clean = deidentify(synthetic_instance(), patient_id="P-42")
        self.assertEqual(clean.RescaleSlope, 1)
        self.assertEqual(clean.RescaleIntercept, -1024)

    def test_patient_id_becomes_the_yggdrasil_identifier(self):
        clean = deidentify(synthetic_instance(with_phi=True), patient_id="P-42")
        self.assertEqual(clean.PatientID, "P-42")
        self.assertEqual(clean.PatientName, ANONYMOUS_NAME)
        self.assertEqual(clean.PatientIdentityRemoved, "YES")

    def test_type_2_elements_are_present_and_empty_rather_than_absent(self):
        clean = deidentify(synthetic_instance(with_phi=True), patient_id="P-42")
        # Present, so a reader demanding them does not reject the file; empty, so they
        # say nothing. Dropping them entirely would trade a leak for a parse failure.
        for keyword in ("StudyDate", "AccessionNumber", "ReferringPhysicianName"):
            self.assertIn(keyword, clean)
            self.assertEqual(clean[keyword].value, "")

    def test_sop_class_uid_is_not_pseudonymised(self):
        clean = deidentify(synthetic_instance(), patient_id="P-42")
        # It names a standard, not an instance. Rewriting it would make the file
        # unreadable to anything that dispatches on it.
        self.assertEqual(clean.SOPClassUID, CTImageStorage)

    def test_kept_keywords_and_the_emitted_set_stay_in_step(self):
        clean = deidentify(synthetic_instance(with_phi=True), patient_id="P-42")
        for keyword in KEPT_KEYWORDS:
            if keyword in ("SmallestImagePixelValue", "LargestImagePixelValue",
                           "NumberOfFrames", "PlanarConfiguration",
                           "SpacingBetweenSlices", "RescaleType",
                           "WindowCenterWidthExplanation", "VOILUTFunction"):
                continue  # optional; the synthetic instance does not carry them
            self.assertIn(keyword, clean, f"{keyword} was dropped")


class RefusalTests(TestCase):
    """Instances header-only de-identification cannot make safe."""

    def test_secondary_capture_is_refused(self):
        ds = synthetic_instance(sop_class_uid="1.2.840.10008.5.1.4.1.1.7")
        self.assertIn("Secondary Capture", refusal_reason(ds))
        with self.assertRaises(DicomRefused):
            deidentify(ds, patient_id="P-42")

    def test_declared_burned_in_annotation_is_refused(self):
        ds = synthetic_instance(burned_in="YES")
        self.assertIn("burned-in", refusal_reason(ds))
        with self.assertRaises(DicomRefused):
            deidentify(ds, patient_id="P-42")

    def test_an_ordinary_ct_is_not_refused(self):
        self.assertIsNone(refusal_reason(synthetic_instance()))

    def test_confidence_never_claims_more_than_the_header_established(self):
        self.assertEqual(
            confidence_for(synthetic_instance(burned_in="NO")),
            CONFIDENCE_DECLARED_CLEAN,
        )
        # The tag is absent far more often than not, and its absence establishes
        # nothing about the pixels.
        self.assertEqual(confidence_for(synthetic_instance()), CONFIDENCE_HEADER_ONLY)


@override_settings(DICOM_UID_HMAC_KEY="test-key")
class PseudonymousUidTests(TestCase):
    """Derived, not mapped: the property that removes the DicomUidMap table."""

    def test_is_deterministic_so_reingest_is_idempotent(self):
        self.assertEqual(
            pseudonymous_uid("1.2.3.4.5"), pseudonymous_uid("1.2.3.4.5")
        )

    def test_distinct_inputs_stay_distinct(self):
        self.assertNotEqual(pseudonymous_uid("1.2.3.4.5"), pseudonymous_uid("1.2.3.4.6"))

    def test_is_a_valid_uid_under_the_iso_derived_arc(self):
        uid = pseudonymous_uid("1.2.826.0.1.3680043.9.7.1")
        self.assertTrue(uid.startswith(f"{UID_ROOT}."))
        self.assertLessEqual(len(uid), 64)
        self.assertTrue(all(part.isdigit() for part in uid.split(".")))

    def test_does_not_contain_the_original(self):
        original = "1.2.826.0.1.3680043.9.7.1"
        self.assertNotIn(original, pseudonymous_uid(original))

    def test_changing_the_key_changes_every_uid(self):
        before = pseudonymous_uid("1.2.3")
        with override_settings(DICOM_UID_HMAC_KEY="another-key"):
            self.assertNotEqual(before, pseudonymous_uid("1.2.3"))

    def test_a_missing_uid_is_refused_rather_than_invented(self):
        with self.assertRaises(DicomRefused):
            pseudonymous_uid("")

    def test_the_derivation_is_hmac_not_a_bare_hash(self):
        # A bare digest would be reversible by anyone with a UID dictionary, which
        # for institutional roots is a small dictionary.
        bare = int.from_bytes(hashlib.sha256(b"1.2.3").digest()[:16], "big")
        self.assertNotEqual(pseudonymous_uid("1.2.3"), f"{UID_ROOT}.{bare}")
