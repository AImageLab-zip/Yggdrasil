"""The measurement endpoint: what it writes, what it refuses, and who may call it.

Two boundaries here are worth more than the happy path.

**A file id and a patient id arrive independently.** Without an explicit check that
the named file belongs to the named patient, a user with write access to patient A
could anchor a measurement set to patient B's volume -- the annotations would be
fingerprinted against a resource nobody expected and the cross-check would later
report drift on a scan that never changed.

**Concurrency is the unique constraint, not a read-modify-write.** Two editors on one
study must not silently overwrite each other; the loser gets a 409 and reloads.
"""

import json
import uuid

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from annotations.models import AnnotationSet, MeasurementItem, SpatialAnnotation3DItem
from annotations.services import current_revision_number
from common.models import FileRegistry, Project, ProjectAccess
from maxillo.models import Folder, Patient


def cornerstone_length(points, tool="Length"):
    return {
        "annotationUID": "runtime-only",
        "metadata": {
            "toolName": tool,
            "FrameOfReferenceUID": "1.2.3.4",
            "referencedImageId": "nifti:https://h/v.nii.gz?frame=3",
        },
        "data": {
            "handles": {"points": points},
            "cachedStats": {"volumeId:x": {"length": 999}},
        },
    }


class MeasurementApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        suffix = uuid.uuid4().hex[:8]
        cls.project = Project.objects.create(
            name=f"meas-{suffix}", slug=f"meas-{suffix}", domain="maxillo"
        )
        cls.folder = Folder.objects.create(name="mx", project=cls.project)
        cls.patient = Patient.objects.create(
            patient_id=9301, folder=cls.folder, project=cls.project
        )
        cls.other_patient = Patient.objects.create(
            patient_id=9302, folder=cls.folder, project=cls.project
        )
        cls.file = FileRegistry.objects.create(
            patient=cls.patient,
            file_type="cbct_raw",
            file_path="maxillo/cbct_raw/scan.nii.gz",
            file_size=1,
            file_hash="0" * 64,
            domain="maxillo",
        )
        cls.other_file = FileRegistry.objects.create(
            patient=cls.other_patient,
            file_type="cbct_raw",
            file_path="maxillo/cbct_raw/other.nii.gz",
            file_size=1,
            file_hash="1" * 64,
            domain="maxillo",
        )

    def setUp(self):
        self.user = self._user(role="annotator")
        self.client.force_login(self.user)
        self.url = reverse(
            "maxillo:api_save_measurements", kwargs={"patient_id": self.patient.patient_id}
        )
        self.state_url = reverse(
            "maxillo:api_measurements_state", kwargs={"patient_id": self.patient.patient_id}
        )

    def _user(self, role="annotator"):
        user = User.objects.create_user(
            username=f"u{uuid.uuid4().hex[:8]}", password="pw"  # noqa: S106
        )
        if role:
            ProjectAccess.objects.create(user=user, project=self.project, role=role)
        return user

    def _post(self, body, url=None):
        return self.client.post(
            url or self.url, data=json.dumps(body), content_type="application/json"
        )

    def _save(self, annotations, **extra):
        return self._post({"fileId": self.file.id, "annotations": annotations, **extra})

    # -- the happy path -----------------------------------------------------

    def test_a_measurement_becomes_geometry_plus_a_recomputed_number(self):
        response = self._save([cornerstone_length([[0, 0, 0], [3, 4, 0]])])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["revision"], 1)

        annotation_set = AnnotationSet.objects.get(patient=self.patient, kind="measurements")
        self.assertTrue(annotation_set.ever_annotated, "human work must freeze the raw data")

        measurement = MeasurementItem.objects.get(revision__annotation_set=annotation_set)
        # The payload claimed 999; the geometry says 5.
        self.assertAlmostEqual(float(measurement.value), 5.0, places=6)
        self.assertEqual(measurement.unit, "mm")
        self.assertTrue(measurement.is_calibrated)

        geometry = SpatialAnnotation3DItem.objects.get(revision__annotation_set=annotation_set)
        self.assertEqual(geometry.frame_of_reference_uid, "1.2.3.4")

    def test_the_resumable_payload_is_stored_and_is_not_canonical(self):
        self._save([cornerstone_length([[0, 0, 0], [1, 0, 0]])])
        annotation_set = AnnotationSet.objects.get(patient=self.patient, kind="measurements")
        payload = annotation_set.revisions.get().payloads.get()

        self.assertEqual(payload.format, "cornerstone_state")
        self.assertIsNone(payload.canonical_slot, "a viewer's state is never canonical")
        # Stripped on the way in: a scratch copy needs no last-session identifiers.
        text = json.dumps(payload.data)
        for key in ("annotationUID", "cachedStats", "referencedImageId"):
            self.assertNotIn(key, text)

    def test_saving_twice_appends_a_revision_rather_than_editing_one(self):
        self._save([cornerstone_length([[0, 0, 0], [3, 4, 0]])])
        second = self._save(
            [cornerstone_length([[0, 0, 0], [6, 8, 0]])], expectedRevision=1
        )
        self.assertEqual(second.json()["revision"], 2)

        annotation_set = AnnotationSet.objects.get(patient=self.patient, kind="measurements")
        self.assertEqual(annotation_set.revisions.count(), 2)
        # Both revisions survive: the history is the audit trail (decision #14).
        self.assertEqual(MeasurementItem.objects.filter(revision__annotation_set=annotation_set).count(), 2)

    def test_an_empty_save_is_how_a_measurement_is_deleted(self):
        """Replace-the-set semantics: the new revision simply holds nothing.

        The previous revision is untouched, so the deletion is recorded rather than
        the work being erased.
        """
        self._save([cornerstone_length([[0, 0, 0], [3, 4, 0]])])
        response = self._save([], expectedRevision=1)
        self.assertEqual(response.status_code, 200)

        annotation_set = AnnotationSet.objects.get(patient=self.patient, kind="measurements")
        latest = annotation_set.revisions.order_by("-revision_number").first()
        self.assertEqual(latest.revision_number, 2)
        self.assertEqual(MeasurementItem.objects.filter(revision=latest).count(), 0)
        self.assertEqual(
            MeasurementItem.objects.filter(revision__annotation_set=annotation_set).count(),
            1,
            "the earlier revision still holds what was there",
        )

    def test_all_the_geometric_tools_round_trip(self):
        annotations = [
            cornerstone_length([[0, 0, 0], [3, 4, 0]], tool="Length"),
            cornerstone_length([[1, 0, 0], [0, 0, 0], [0, 1, 0]], tool="Angle"),
            cornerstone_length([[0, 0, 0], [4, 0, 0], [0, 3, 0], [4, 3, 0]], tool="RectangleROI"),
            cornerstone_length([[0, 0, 0], [0, 5, 0]], tool="CircleROI"),
            cornerstone_length([[1, 2, 3]], tool="Probe"),
        ]
        response = self._save(annotations)
        self.assertEqual(response.status_code, 200, response.content)

        annotation_set = AnnotationSet.objects.get(patient=self.patient, kind="measurements")
        revision = annotation_set.revisions.get()
        self.assertEqual(SpatialAnnotation3DItem.objects.filter(revision=revision).count(), 5)
        # Probe contributes geometry and no number; the rest contribute 1, 1, 2, 2.
        self.assertEqual(MeasurementItem.objects.filter(revision=revision).count(), 6)

    # -- concurrency --------------------------------------------------------

    def test_a_stale_expected_revision_is_a_409(self):
        self._save([cornerstone_length([[0, 0, 0], [1, 0, 0]])])  # revision 1

        # A second editor who loaded revision 0 tries to save.
        response = self._save([cornerstone_length([[0, 0, 0], [2, 0, 0]])], expectedRevision=0)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["conflict"])

        annotation_set = AnnotationSet.objects.get(patient=self.patient, kind="measurements")
        self.assertEqual(current_revision_number(annotation_set), 1, "the winner is untouched")

    def test_the_state_endpoint_tells_a_client_what_to_quote(self):
        """Guessing zero means every second editor loses a 409 they could avoid."""
        before = self.client.get(self.state_url).json()
        self.assertEqual(before["revision"], 0)
        self.assertIsNone(before["setId"])

        self._save([cornerstone_length([[0, 0, 0], [1, 0, 0]])])
        after = self.client.get(self.state_url).json()
        self.assertEqual(after["revision"], 1)
        self.assertTrue(after["everAnnotated"])

        # And quoting it works.
        self.assertEqual(
            self._save([], expectedRevision=after["revision"]).status_code, 200
        )

    # -- the boundaries -----------------------------------------------------

    def test_a_file_belonging_to_another_patient_is_refused(self):
        """The file id and the patient id arrive independently, so this is checked.

        Without it, write access to one patient would let a caller anchor a
        measurement set to a different patient's volume.
        """
        response = self._post(
            {"fileId": self.other_file.id, "annotations": [cornerstone_length([[0, 0, 0], [1, 0, 0]])]}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AnnotationSet.objects.filter(patient=self.patient).exists())

    def test_a_user_without_write_access_is_refused(self):
        self.client.force_login(self._user(role="viewer"))
        self.assertEqual(
            self._save([cornerstone_length([[0, 0, 0], [1, 0, 0]])]).status_code, 403
        )
        self.assertFalse(AnnotationSet.objects.filter(patient=self.patient).exists())

    def test_anonymous_callers_are_redirected(self):
        self.client.logout()
        self.assertEqual(self._save([]).status_code, 302)

    def test_the_endpoint_is_post_only(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    # -- refusals -----------------------------------------------------------

    def test_an_unmapped_tool_aborts_the_whole_save(self):
        """Translation runs before the first row, so nothing partial is left behind."""
        response = self._save(
            [
                cornerstone_length([[0, 0, 0], [3, 4, 0]]),
                cornerstone_length([[0, 0, 0]], tool="ArrowAnnotate"),
            ]
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("no descriptor mapping", response.json()["error"])
        self.assertFalse(
            AnnotationSet.objects.filter(patient=self.patient, kind="measurements").exists(),
            "a refused save must leave no revision at all",
        )

    def test_an_incomplete_annotation_aborts_the_save(self):
        response = self._save([cornerstone_length([[0, 0, 0], [1, 0, 0]], tool="Angle")])
        self.assertEqual(response.status_code, 400)
        self.assertIn("exactly 3 handles", response.json()["error"])

    def test_a_non_finite_coordinate_is_refused(self):
        # Sent as a literal, the way a JS client would serialise a NaN it did not
        # notice. json.dumps emits bare NaN, which Django's parser accepts.
        body = json.dumps(
            {"fileId": self.file.id, "annotations": [cornerstone_length([[0, 0, 0], [float("nan"), 0, 0]])]}
        )
        response = self.client.post(self.url, data=body, content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_an_oversized_save_is_refused_rather_than_written(self):
        """A client resending its buffer in a loop must not become an unbounded write."""
        annotations = [cornerstone_length([[0, 0, 0], [1, 0, 0]])] * 501
        response = self._save(annotations)
        self.assertEqual(response.status_code, 400)
        self.assertIn("exceeds", response.json()["error"])

    def test_malformed_bodies_are_400s(self):
        self.assertEqual(
            self.client.post(self.url, data="not json", content_type="application/json").status_code,
            400,
        )
        self.assertEqual(self._post({"annotations": []}).status_code, 400)  # no fileId
        self.assertEqual(self._post({"fileId": "x", "annotations": []}).status_code, 400)
        self.assertEqual(self._post({"fileId": self.file.id}).status_code, 400)  # no list
        self.assertEqual(
            self._post({"fileId": self.file.id, "annotations": [], "expectedRevision": "x"}).status_code,
            400,
        )

    def test_an_unknown_coordinate_system_is_refused(self):
        response = self._save(
            [cornerstone_length([[0, 0, 0], [1, 0, 0]])], coordinateSystem="patient_world"
        )
        self.assertEqual(response.status_code, 400)

    def test_a_voxel_frame_yields_pixels_not_millimetres(self):
        """The calibration rule reaches the database through the endpoint too."""
        response = self._save(
            [cornerstone_length([[0, 0, 0], [3, 4, 0]])], coordinateSystem="volume_voxel"
        )
        self.assertEqual(response.status_code, 200, response.content)
        measurement = MeasurementItem.objects.get()
        self.assertEqual(measurement.unit, "px")
        self.assertFalse(measurement.is_calibrated)
