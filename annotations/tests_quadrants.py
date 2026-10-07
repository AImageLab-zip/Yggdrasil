"""The video quadrant timeline: its own vocabulary, snapshot saves, refusals.

Pinned because each is a way this goes quietly wrong: sharing the segmentation schema,
a stale save overwriting, two markers on one instant, a foreign patient's file.
"""

import json
import uuid

from django.apps import apps
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from annotations.models import AnnotationSet, LabelSchema
from common.models import AnnotationMethod, FileRegistry, Project, ProjectAccess

# Through the registry: annotations must not import a domain app (lint-imports).
Folder = apps.get_model("laparoscopy", "Folder")
Patient = apps.get_model("laparoscopy", "Patient")


class QuadrantApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        suffix = uuid.uuid4().hex[:8]
        cls.project = Project.objects.create(
            name=f"quad-{suffix}", slug=f"quad-{suffix}", domain="laparoscopy"
        )
        cls.project.annotation_methods.set(
            AnnotationMethod.objects.filter(slug="image_segmentation")
        )
        folder = Folder.objects.create(name="F", project=cls.project)
        cls.patient = Patient.objects.create(name="P", folder=folder, project=cls.project)
        other = Patient.objects.create(name="Q", folder=folder, project=cls.project)
        cls.video = cls._video(cls.patient, "case.mp4", "a")
        cls.foreign_video = cls._video(other, "other.mp4", "b")

    @classmethod
    def _video(cls, patient, name, hash_char):
        return FileRegistry.objects.create(
            domain="laparoscopy",
            file_type="video_raw",
            laparoscopy_patient=patient,
            file_path=f"laparoscopy/raw/{name}",
            file_size=1,
            file_hash=hash_char * 64,
        )

    def setUp(self):
        self.user = User.objects.create_user(username=f"u{uuid.uuid4().hex[:8]}", password="pw")  # noqa: S106
        ProjectAccess.objects.create(user=self.user, project=self.project, role="annotator")
        self.client.force_login(self.user)
        kw = {"patient_id": self.patient.patient_id}
        self.url = reverse("laparoscopy:api_quadrants", kwargs=kw)
        self.labels_url = reverse("laparoscopy:api_quadrant_labels", kwargs=kw)
        self.seg_labels_url = reverse("laparoscopy:api_image_segmentation_labels", kwargs=kw)
        self.upper = self._label("Upper left")
        self.lower = self._label("Lower right")

    def _label(self, name):
        response = self.client.post(
            self.labels_url, json.dumps({"name": name}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["label"]["code"]

    def _put(self, markers, *, expected=None, file_id=None):
        if expected is None:
            expected = self.client.get(self.url).json()["revision"]
        body = {"fileId": file_id or self.video.pk, "expectedRevision": expected, "markers": markers}
        return self.client.put(self.url, json.dumps(body), content_type="application/json")

    def test_the_vocabulary_is_separate_from_the_segmentation_labels(self):
        self.client.post(
            self.seg_labels_url, json.dumps({"name": "Liver"}), content_type="application/json"
        )
        quadrants = self.client.get(self.url).json()["labels"]
        regions = self.client.get(self.seg_labels_url).json()["labels"]
        self.assertEqual([label["name"] for label in quadrants], ["Upper left", "Lower right"])
        self.assertEqual([label["name"] for label in regions], ["Liver"])
        self.assertEqual(
            set(LabelSchema.objects.filter(slug__contains="-project-").values_list("slug", flat=True)),
            {f"quadrant-project-{self.project.pk}", f"image-segmentation-project-{self.project.pk}"},
        )

    def test_markers_round_trip_sorted_and_each_save_is_a_snapshot(self):
        response = self._put([{"timeMs": 5000, "code": self.lower}, {"timeMs": 0, "code": self.upper}])
        self.assertEqual(response.status_code, 200, response.content)
        state = response.json()
        self.assertEqual(state["revision"], 1)
        self.assertEqual(
            state["markers"],
            [{"timeMs": 0, "code": self.upper}, {"timeMs": 5000, "code": self.lower}],
        )
        state = self._put([{"timeMs": 0, "code": self.lower}]).json()
        self.assertEqual((state["revision"], state["markers"]), (2, [{"timeMs": 0, "code": self.lower}]))
        self.assertEqual(AnnotationSet.objects.filter(kind="video_quadrants").count(), 1)

    def test_an_empty_list_clears_the_timeline(self):
        self._put([{"timeMs": 0, "code": self.upper}])
        self.assertEqual(self._put([]).json()["markers"], [])

    def test_a_stale_revision_is_a_conflict(self):
        self._put([{"timeMs": 0, "code": self.upper}])
        response = self._put([], expected=0)
        self.assertEqual(response.status_code, 409)

    def test_two_markers_on_one_instant_are_refused_and_nothing_is_written(self):
        response = self._put([{"timeMs": 10, "code": self.upper}, {"timeMs": 10, "code": self.lower}])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get(self.url).json()["revision"], 0)

    def test_bad_markers_are_refused(self):
        cases = {
            "unknown quadrant": [{"timeMs": 0, "code": "l99"}],
            "float time": [{"timeMs": 1.5, "code": self.upper}],
            "negative time": [{"timeMs": -1, "code": self.upper}],
            "not an object": ["x"],
        }
        for name, markers in cases.items():
            with self.subTest(name):
                self.assertEqual(self._put(markers).status_code, 400)

    def test_another_patients_file_is_refused(self):
        self.assertEqual(self._put([], file_id=self.foreign_video.pk).status_code, 403)

    def test_a_retired_quadrant_keeps_its_markers(self):
        self._put([{"timeMs": 0, "code": self.upper}])
        url = reverse(
            "laparoscopy:api_quadrant_label",
            kwargs={"patient_id": self.patient.patient_id, "code": self.upper},
        )
        self.assertFalse(self.client.delete(url).json()["label"]["active"])
        state = self.client.get(self.client.get(self.url).wsgi_request.path).json()
        self.assertEqual(state["markers"], [{"timeMs": 0, "code": self.upper}])
        self.assertEqual(self._put(state["markers"]).status_code, 200)

    def test_a_project_without_the_method_cannot_save(self):
        self.project.annotation_methods.clear()
        self.assertEqual(self._put([]).status_code, 403)
