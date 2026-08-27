"""Saving when a patient owns more than one annotatable resource.

The defect these tests exist for is not a photo-stack defect. ``AnnotationSet`` is keyed
``(domain, patient, kind)`` and a revision replaces the whole set, so *any* patient with
two annotatable resources loses one when the other is saved -- a brain patient with two
series does it today, without a photo stack anywhere near it. A stack of N images only
makes it easy to reach.

The fix is: replace the resources the save names, carry the rest forward, both on one
revision. So the cases that matter are the ones about what happens to the resource the
save did *not* mention, and the ones that make sure "carry forward" never becomes
"resurrect what the user deleted".
"""

import json
import uuid

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from annotations.models import (
    AnnotationSet,
    Geometry2DItem,
    MeasurementItem,
    SpatialAnnotation3DItem,
)
from common.models import FileRegistry, Project, ProjectAccess
from maxillo.models import Folder, Patient


def length_annotation(points, tool="Length"):
    """One Cornerstone annotation, carrying the runtime junk a real one carries."""
    return {
        "annotationUID": "runtime-only",
        "metadata": {"toolName": tool, "FrameOfReferenceUID": "1.2.3.4"},
        "data": {
            "handles": {"points": points},
            "cachedStats": {"volumeId:x": {"length": 999}},
        },
    }


class MultiTargetSaveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        suffix = uuid.uuid4().hex[:8]
        cls.project = Project.objects.create(
            name=f"mt-{suffix}", slug=f"mt-{suffix}", domain="maxillo"
        )
        cls.folder = Folder.objects.create(name="mx", project=cls.project)
        cls.patient = Patient.objects.create(
            patient_id=9401, folder=cls.folder, project=cls.project
        )
        cls.other_patient = Patient.objects.create(
            patient_id=9402, folder=cls.folder, project=cls.project
        )
        cls.volume = cls._file(cls.patient, "cbct_raw", "scan.nii.gz", "0")
        cls.photo_a = cls._file(cls.patient, "intraoral_raw", "a.jpg", "a")
        cls.photo_b = cls._file(cls.patient, "intraoral_raw", "b.jpg", "b")
        cls.foreign = cls._file(cls.other_patient, "intraoral_raw", "x.jpg", "x")

    @classmethod
    def _file(cls, patient, file_type, name, hash_char):
        return FileRegistry.objects.create(
            patient=patient,
            file_type=file_type,
            file_path=f"maxillo/{file_type}/{name}",
            file_size=1,
            file_hash=hash_char * 64,
            domain="maxillo",
        )

    def setUp(self):
        self.user = User.objects.create_user(
            username=f"u{uuid.uuid4().hex[:8]}", password="pw"  # noqa: S106
        )
        ProjectAccess.objects.create(user=self.user, project=self.project, role="annotator")
        self.client.force_login(self.user)
        self.url = reverse(
            "maxillo:api_save_measurements", kwargs={"patient_id": self.patient.patient_id}
        )
        self.state_url = reverse(
            "maxillo:api_measurements_state", kwargs={"patient_id": self.patient.patient_id}
        )

    def _post(self, body):
        return self.client.post(
            self.url, data=json.dumps(body), content_type="application/json"
        )

    def _save_images(self, images, **extra):
        return self._post({"images": images, "coordinateSystem": "image_pixel", **extra})

    def _image(self, file_obj, points_list):
        return {
            "fileId": file_obj.id,
            "annotations": [length_annotation(points) for points in points_list],
        }

    def _set(self):
        return AnnotationSet.objects.get(patient=self.patient, kind="measurements")

    def _latest(self):
        return self._set().revisions.order_by("-revision_number").first()

    # -- one revision, several resources ------------------------------------

    def test_a_two_image_save_is_one_revision_with_two_targets(self):
        response = self._save_images(
            [
                self._image(self.photo_a, [[[0, 0], [3, 4]]]),
                self._image(self.photo_b, [[[0, 0], [6, 8]]]),
            ]
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["revision"], 1)
        self.assertEqual(response.json()["annotations"], 2)

        annotation_set = self._set()
        self.assertEqual(annotation_set.revisions.count(), 1, "one save is one revision")
        self.assertEqual(annotation_set.targets.count(), 2)

        revision = self._latest()
        by_file = {
            item.target.source_resource.file_id: float(item.value)
            for item in MeasurementItem.objects.filter(revision=revision)
        }
        self.assertEqual(
            by_file, {self.photo_a.id: 5.0, self.photo_b.id: 10.0},
            "each image's number must be computed from its own handles",
        )

    def test_a_photo_becomes_a_2d_geometry_in_pixels_and_is_not_calibrated(self):
        self._save_images([self._image(self.photo_a, [[[0, 0], [3, 4]]])])
        revision = self._latest()

        geometry = Geometry2DItem.objects.get(revision=revision)
        self.assertEqual(geometry.coordinate_system, "image_pixel")
        self.assertEqual(geometry.points, [[0.0, 0.0], [3.0, 4.0]])
        self.assertFalse(
            SpatialAnnotation3DItem.objects.filter(revision=revision).exists(),
            "a planar frame must not produce a three-space row",
        )

        measurement = MeasurementItem.objects.get(revision=revision)
        self.assertAlmostEqual(float(measurement.value), 5.0, places=6)
        self.assertEqual(measurement.unit, "px")
        self.assertFalse(
            measurement.is_calibrated,
            "a photograph has no millimetre scale until somebody measures one",
        )

    # -- the actual bug -----------------------------------------------------

    def test_saving_one_image_carries_the_others_work_forward(self):
        """The case the whole change exists for."""
        self._save_images(
            [
                self._image(self.photo_a, [[[0, 0], [3, 4]]]),
                self._image(self.photo_b, [[[0, 0], [6, 8]]]),
            ]
        )
        # Now edit only image A, as a viewer showing one image at a time would.
        response = self._save_images(
            [self._image(self.photo_a, [[[0, 0], [5, 12]]])], expectedRevision=1
        )
        self.assertEqual(response.status_code, 200, response.content)

        revision = self._latest()
        self.assertEqual(revision.revision_number, 2)
        by_file = {
            item.target.source_resource.file_id: float(item.value)
            for item in MeasurementItem.objects.filter(revision=revision)
        }
        self.assertEqual(
            by_file,
            {self.photo_a.id: 13.0, self.photo_b.id: 10.0},
            "A is replaced; B is carried forward untouched",
        )

    def test_the_carried_forward_payload_matches_the_carried_forward_rows(self):
        """A resume point that disagrees with the record is how work looks lost."""
        self._save_images(
            [
                self._image(self.photo_a, [[[0, 0], [3, 4]]]),
                self._image(self.photo_b, [[[0, 0], [6, 8]]]),
            ]
        )
        self._save_images(
            [self._image(self.photo_a, [[[0, 0], [5, 12]]])], expectedRevision=1
        )

        state = self.client.get(
            self.state_url, {"fileIds": f"{self.photo_a.id},{self.photo_b.id}"}
        ).json()
        counts = {entry["fileId"]: len(entry["annotations"]) for entry in state["images"]}
        self.assertEqual(counts, {self.photo_a.id: 1, self.photo_b.id: 1})

    def test_an_empty_save_deletes_only_the_image_it_names(self):
        self._save_images(
            [
                self._image(self.photo_a, [[[0, 0], [3, 4]]]),
                self._image(self.photo_b, [[[0, 0], [6, 8]]]),
            ]
        )
        self._save_images([self._image(self.photo_a, [])], expectedRevision=1)

        revision = self._latest()
        by_file = {
            item.target.source_resource.file_id: float(item.value)
            for item in MeasurementItem.objects.filter(revision=revision)
        }
        self.assertEqual(
            by_file, {self.photo_b.id: 10.0},
            "clearing A must not clear B, and must not leave A behind",
        )

    def test_a_deletion_stays_deleted_across_a_save_of_the_other_image(self):
        """Carry-forward must copy the latest state, never an older one."""
        self._save_images([self._image(self.photo_a, [[[0, 0], [3, 4]]])])
        self._save_images([self._image(self.photo_a, [])], expectedRevision=1)
        self._save_images([self._image(self.photo_b, [[[0, 0], [6, 8]]])], expectedRevision=2)

        revision = self._latest()
        self.assertEqual(
            [float(item.value) for item in MeasurementItem.objects.filter(revision=revision)],
            [10.0],
            "A was deleted two revisions ago and must not come back",
        )

    def test_a_volume_and_a_photo_coexist_in_one_set(self):
        """The latent defect, in the shape it already has today."""
        self._post(
            {"fileId": self.volume.id, "annotations": [length_annotation([[0, 0, 0], [3, 4, 0]])]}
        )
        self._save_images(
            [self._image(self.photo_a, [[[0, 0], [6, 8]]])], expectedRevision=1
        )

        revision = self._latest()
        self.assertEqual(
            SpatialAnnotation3DItem.objects.filter(revision=revision).count(), 1,
            "the CBCT's patient-space geometry survives a photo save",
        )
        self.assertEqual(Geometry2DItem.objects.filter(revision=revision).count(), 1)

    def test_a_photo_save_does_not_steal_the_primary_slot_from_the_volume(self):
        self._post(
            {"fileId": self.volume.id, "annotations": [length_annotation([[0, 0, 0], [3, 4, 0]])]}
        )
        self._save_images(
            [self._image(self.photo_a, [[[0, 0], [6, 8]]])], expectedRevision=1
        )

        primary = self._set().targets.get(primary_slot=1)
        self.assertEqual(
            primary.source_resource.file_id, self.volume.id,
            "the primary slot answers 'what is this set mostly about', not 'what was "
            "saved last'",
        )

    # -- refusals -----------------------------------------------------------

    def test_a_group_naming_another_patients_file_writes_nothing(self):
        response = self._save_images(
            [
                self._image(self.photo_a, [[[0, 0], [3, 4]]]),
                self._image(self.foreign, [[[0, 0], [6, 8]]]),
            ]
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            AnnotationSet.objects.filter(patient=self.patient).exists(),
            "one bad group must abort the save before any row exists",
        )

    def test_sending_both_body_shapes_is_refused_rather_than_resolved(self):
        response = self._post(
            {
                "fileId": self.volume.id,
                "annotations": [],
                "images": [self._image(self.photo_a, [])],
            }
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not both", response.json()["error"])

    def test_naming_one_resource_twice_in_a_save_is_refused(self):
        response = self._save_images(
            [
                self._image(self.photo_a, [[[0, 0], [3, 4]]]),
                self._image(self.photo_a, [[[0, 0], [6, 8]]]),
            ]
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already in this save", response.json()["error"])

    def test_the_annotation_cap_counts_the_whole_save_not_each_group(self):
        half = [[[0, 0], [1, 1]]] * 300
        response = self._save_images(
            [self._image(self.photo_a, half), self._image(self.photo_b, half)]
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("600 annotations", response.json()["error"])

    def test_a_stale_expected_revision_is_still_a_409(self):
        self._save_images([self._image(self.photo_a, [[[0, 0], [3, 4]]])])
        response = self._save_images(
            [self._image(self.photo_a, [[[0, 0], [6, 8]]])], expectedRevision=0
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["conflict"])

    def test_a_three_ordinate_handle_in_a_planar_frame_is_refused(self):
        """Not truncated. A third number means the caller converted nothing."""
        response = self._save_images([self._image(self.photo_a, [[[0, 0, 0], [3, 4, 0]]])])
        self.assertEqual(response.status_code, 400)
        self.assertIn("2 coordinates", response.json()["error"])

    def test_an_empty_images_list_is_refused(self):
        self.assertEqual(self._save_images([]).status_code, 400)

    # -- the narrowing the volume grid needs --------------------------------

    def test_the_state_endpoint_is_unchanged_when_nothing_is_narrowed(self):
        self._post(
            {"fileId": self.volume.id, "annotations": [length_annotation([[0, 0, 0], [3, 4, 0]])]}
        )
        state = self.client.get(self.state_url).json()
        self.assertEqual(len(state["annotations"]), 1)
        self.assertNotIn(
            "annotationUID", json.dumps(state["annotations"]),
            "runtime identifiers must not survive the round trip",
        )

    def test_narrowing_by_file_id_returns_only_that_resources_work(self):
        self._save_images(
            [
                self._image(self.photo_a, [[[0, 0], [3, 4]]]),
                self._image(self.photo_b, [[[0, 0], [6, 8]]]),
            ]
        )
        state = self.client.get(self.state_url, {"fileId": self.photo_b.id}).json()
        self.assertEqual(len(state["annotations"]), 1)
        self.assertEqual(
            state["annotations"][0]["data"]["handles"]["points"], [[0, 0], [6, 8]]
        )

    def test_an_unannotated_file_id_gets_an_empty_list_not_someone_elses(self):
        self._save_images([self._image(self.photo_a, [[[0, 0], [3, 4]]])])
        state = self.client.get(self.state_url, {"fileId": self.photo_b.id}).json()
        self.assertEqual(state["annotations"], [])

    def test_narrowing_a_patient_with_no_set_still_returns_the_narrowed_shape(self):
        state = self.client.get(self.state_url, {"fileIds": str(self.photo_a.id)}).json()
        self.assertEqual(
            state["images"], [{"fileId": self.photo_a.id, "fileKey": None, "annotations": []}]
        )

    def test_a_non_numeric_file_id_is_a_400(self):
        self.assertEqual(
            self.client.get(self.state_url, {"fileIds": "1,nope"}).status_code, 400
        )
