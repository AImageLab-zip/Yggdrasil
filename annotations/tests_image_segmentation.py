"""2D labelmap segmentation: the codec, the validators, the service and its HTTP surface.

The properties worth pinning, because each is a way this goes quietly wrong:

- **Overlap survives.** Labels are independent binary planes; a single-valued labelmap
  would have kept only one of two overlapping labels and looked fine.
- **Carry-forward moves no bytes.** A save names only the frames it edited; the rest are
  new payload rows over the *same* files. A copy would be correct and expensive.
- **A frame with no pixels is a deletion**, not an empty file.
- **A refused save leaves nothing behind** -- neither rows nor objects in storage.
- **Renaming a label cannot move a mask**, because planes are keyed by a code that never
  changes.
"""

import base64
import json
import uuid
import zlib
from unittest import mock

import numpy as np
from django.apps import apps
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from annotations.adapters import image_segmentation as codec
from annotations.models import AnnotationPayload, AnnotationSet
from annotations.validators.image_segmentation import (
    MAX_FRAMES_PER_SAVE,
    validate_frame_batch,
)
from common.annotation_lock import raw_data_is_locked
from common.models import AnnotationMethod, FileRegistry, Project, ProjectAccess
from common.object_storage import get_object_storage

# Through the registry: annotations must not import a domain app (lint-imports).
Folder = apps.get_model("laparoscopy", "Folder")
Patient = apps.get_model("laparoscopy", "Patient")

W, H = 64, 48


def rect(x0, y0, x1, y1, width=W, height=H):
    plane = np.zeros((height, width), dtype=np.uint8)
    plane[y0:y1, x0:x1] = 1
    return plane


def frame(time_ms, planes, width=W, height=H):
    """A wire-format frame: ``{code: ndarray}`` encoded the way the browser sends them."""
    return {
        "timeMs": time_ms,
        "width": width,
        "height": height,
        "planes": {code: codec.encode_plane(plane) for code, plane in planes.items()},
    }


class CodecTests(SimpleTestCase):
    def test_a_plane_survives_the_wire(self):
        plane = rect(5, 6, 20, 30)
        self.assertTrue(np.array_equal(codec.decode_plane(codec.encode_plane(plane), W, H), plane))

    def test_a_plane_of_the_wrong_size_is_refused(self):
        encoded = codec.encode_plane(rect(0, 0, 4, 4))
        with self.assertRaisesMessage(ValidationError, "exactly"):
            codec.decode_plane(encoded, W + 1, H)

    def test_values_other_than_zero_and_one_are_refused(self):
        plane = rect(0, 0, 4, 4)
        plane[0, 0] = 2
        with self.assertRaisesMessage(ValidationError, "0 or 1"):
            codec.decode_plane(codec.encode_plane(plane), W, H)

    def test_garbage_is_a_validation_error_not_a_crash(self):
        for bad in ("!!!", base64.b64encode(b"not gzip").decode(), None):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                codec.decode_plane(bad, W, H)

    def test_a_decompression_bomb_is_refused_without_inflating_it(self):
        compressor = zlib.compressobj(wbits=31)
        bomb = compressor.compress(b"\x00" * (50 * 1024 * 1024)) + compressor.flush()
        with self.assertRaises(ValidationError):
            codec.decode_plane(base64.b64encode(bomb).decode(), W, H)

    def test_overlapping_planes_both_survive_the_archive(self):
        liver, tumour = rect(0, 0, 40, 30), rect(10, 10, 20, 20)
        width, height, planes = codec.npz_to_planes(
            codec.planes_to_npz(W, H, {"l1": liver, "l2": tumour})
        )
        self.assertEqual((width, height), (W, H))
        self.assertTrue(np.array_equal(planes["l1"], liver))
        self.assertTrue(np.array_equal(planes["l2"], tumour))
        self.assertTrue((planes["l1"] & planes["l2"]).any(), "the overlap must be kept")

    def test_an_empty_plane_is_not_stored(self):
        _, _, planes = codec.npz_to_planes(
            codec.planes_to_npz(W, H, {"l1": rect(0, 0, 4, 4), "l2": rect(0, 0, 0, 0)})
        )
        self.assertEqual(set(planes), {"l1"})

    def test_stats_are_inclusive_pixel_bounds(self):
        self.assertEqual(
            codec.plane_stats(rect(2, 3, 6, 9)), {"area": 24, "bbox": [2, 3, 5, 8]}
        )
        self.assertIsNone(codec.plane_stats(rect(0, 0, 0, 0)))

    def test_a_variant_names_its_source_and_time(self):
        self.assertEqual(codec.frame_variant(12, 3400), "f12t3400")
        self.assertEqual(codec.parse_variant("f12t3400"), (12, 3400))
        self.assertIsNone(codec.parse_variant("t3400"))


class ValidatorTests(SimpleTestCase):
    def _batch(self, **overrides):
        entry = {"time_ms": 0, "width": W, "height": H, "planes": {"l1": "x"}}
        entry.update(overrides)
        return [entry]

    def test_a_well_formed_batch_passes(self):
        validate_frame_batch(self._batch())

    def test_each_structural_fault_is_refused(self):
        cases = {
            "empty": [],
            "not a list": {"a": 1},
            "float time": self._batch(time_ms=1.5),
            "negative time": self._batch(time_ms=-1),
            "bool time": self._batch(time_ms=True),
            "zero width": self._batch(width=0),
            "huge": self._batch(width=9000),
            "bad code": self._batch(planes={"../x": "x"}),
            "planes not a dict": self._batch(planes=["l1"]),
        }
        for name, batch in cases.items():
            with self.subTest(name), self.assertRaises(ValidationError):
                validate_frame_batch(batch)

    def test_a_repeated_time_is_refused(self):
        batch = self._batch() + self._batch()
        with self.assertRaisesMessage(ValidationError, "repeats"):
            validate_frame_batch(batch)

    def test_too_many_frames_are_refused(self):
        batch = [
            {"time_ms": i, "width": W, "height": H, "planes": {}}
            for i in range(MAX_FRAMES_PER_SAVE + 1)
        ]
        with self.assertRaisesMessage(ValidationError, "at most"):
            validate_frame_batch(batch)


class ImageSegmentationApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        suffix = uuid.uuid4().hex[:8]
        cls.project = Project.objects.create(
            name=f"imgseg-{suffix}", slug=f"imgseg-{suffix}", domain="laparoscopy"
        )
        cls.project.annotation_methods.set(
            AnnotationMethod.objects.filter(slug="image_segmentation")
        )
        cls.folder = Folder.objects.create(name="F", project=cls.project)
        cls.patient = Patient.objects.create(name="P", folder=cls.folder, project=cls.project)
        cls.other = Patient.objects.create(name="Q", folder=cls.folder, project=cls.project)
        cls.video = cls._video(cls.patient, "case.mp4", "a")
        cls.foreign_video = cls._video(cls.other, "other.mp4", "b")

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
        self.save_url = reverse("laparoscopy:api_save_image_segmentation", kwargs=kw)
        self.state_url = reverse("laparoscopy:api_image_segmentation_state", kwargs=kw)
        self.frame_url = reverse("laparoscopy:api_image_segmentation_frame", kwargs=kw)
        self.labels_url = reverse("laparoscopy:api_image_segmentation_labels", kwargs=kw)
        self.liver = self._label("Liver")
        self.tumour = self._label("Tumour")

    # -- helpers ------------------------------------------------------------

    def _label(self, name):
        response = self.client.post(
            self.labels_url, json.dumps({"name": name}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["label"]["code"]

    def _state(self):
        response = self.client.get(self.state_url)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _save(self, frames, *, expected=None, file_id=None, **extra):
        state_revision = self._state()["revision"] if expected is None else expected
        body = {
            "fileId": file_id or self.video.pk,
            "expectedRevision": state_revision,
            "coordinateSystem": "video_pixel",
            "frames": frames,
            **extra,
        }
        return self.client.post(self.save_url, json.dumps(body), content_type="application/json")

    def _read(self, time_ms):
        response = self.client.get(
            self.frame_url, {"fileId": self.video.pk, "timeMs": time_ms}
        )
        return response

    def _mask_rows(self):
        return FileRegistry.objects.filter(file_type="annotation_mask")

    # -- vocabulary ---------------------------------------------------------

    def test_labels_get_stable_codes_and_distinct_colours(self):
        labels = self.client.get(self.labels_url).json()["labels"]
        self.assertEqual([label["code"] for label in labels], ["l1", "l2"])
        self.assertNotEqual(labels[0]["color"], labels[1]["color"])

    def test_a_duplicate_name_is_refused_case_insensitively(self):
        response = self.client.post(
            self.labels_url, json.dumps({"name": "liver"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_a_rename_changes_the_name_and_never_the_code_or_value(self):
        url = reverse(
            "laparoscopy:api_image_segmentation_label",
            kwargs={"patient_id": self.patient.patient_id, "code": self.liver},
        )
        response = self.client.patch(
            url, json.dumps({"name": "Hepar", "color": "#112233"}), content_type="application/json"
        )
        label = response.json()["label"]
        self.assertEqual((label["code"], label["value"], label["name"], label["color"]), (self.liver, 1, "Hepar", "#112233"))

    def test_a_retired_label_is_kept_and_its_value_is_not_reused(self):
        url = reverse(
            "laparoscopy:api_image_segmentation_label",
            kwargs={"patient_id": self.patient.patient_id, "code": self.tumour},
        )
        self.assertFalse(self.client.delete(url).json()["label"]["active"])
        self.assertEqual(self._label("Spleen"), "l3", "retiring must not free a value")
        labels = {label["code"]: label for label in self._state()["labels"]}
        self.assertFalse(labels[self.tumour]["active"])

    # -- saving and reading -------------------------------------------------

    def test_a_frame_round_trips_through_save_state_and_read(self):
        mask = rect(5, 5, 30, 20)
        response = self._save([frame(4200, {self.liver: mask})], fps=25.0)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["revision"], 1)

        state = self._state()
        self.assertEqual(state["revision"], 1)
        entry = state["sources"][str(self.video.pk)]
        self.assertEqual(entry["coordinateSystem"], "video_pixel")
        self.assertEqual(entry["fps"], 25.0)
        self.assertEqual(entry["frames"]["4200"]["labels"][self.liver]["bbox"], [5, 5, 29, 19])

        read = self._read(4200).json()
        got = codec.decode_plane(read["planes"][self.liver], read["width"], read["height"])
        self.assertTrue(np.array_equal(got, mask))

    def test_overlapping_labels_both_come_back(self):
        liver, tumour = rect(0, 0, 40, 30), rect(10, 10, 20, 20)
        self._save([frame(0, {self.liver: liver, self.tumour: tumour})])
        read = self._read(0).json()
        for code, expected in ((self.liver, liver), (self.tumour, tumour)):
            got = codec.decode_plane(read["planes"][code], read["width"], read["height"])
            self.assertTrue(np.array_equal(got, expected), code)

    def test_a_save_creates_a_set_of_its_own_kind_and_locks_the_patient(self):
        self._save([frame(0, {self.liver: rect(0, 0, 4, 4)})])
        annotation_set = AnnotationSet.objects.get(
            laparoscopy_patient=self.patient, kind="image_segmentation"
        )
        self.assertTrue(annotation_set.ever_annotated)
        self.assertTrue(raw_data_is_locked(self.patient))
        self.assertEqual(annotation_set.label_schema.slug, f"image-segmentation-project-{self.project.pk}")

    def test_unedited_frames_are_carried_forward_without_copying_bytes(self):
        self._save([frame(1000, {self.liver: rect(0, 0, 8, 8)})])
        first_file = AnnotationPayload.objects.get(
            revision__revision_number=1, variant=codec.frame_variant(self.video.pk, 1000)
        ).file_id
        files_before = self._mask_rows().count()

        self._save([frame(2000, {self.liver: rect(1, 1, 9, 9)})])

        revision_two = AnnotationPayload.objects.filter(revision__revision_number=2, format="npz_mask")
        self.assertEqual(revision_two.count(), 2)
        carried = revision_two.get(variant=codec.frame_variant(self.video.pk, 1000))
        self.assertEqual(carried.file_id, first_file, "same file, not a copy")
        self.assertEqual(self._mask_rows().count(), files_before + 1, "only the new frame wrote bytes")
        self.assertEqual(set(self._state()["sources"][str(self.video.pk)]["frames"]), {"1000", "2000"})

    def test_editing_a_frame_replaces_it_and_keeps_the_old_revision_intact(self):
        self._save([frame(0, {self.liver: rect(0, 0, 8, 8)})])
        self._save([frame(0, {self.liver: rect(0, 0, 16, 16)})])
        self.assertEqual(self._state()["sources"][str(self.video.pk)]["frames"]["0"]["labels"][self.liver]["area"], 256)
        old = AnnotationPayload.objects.get(revision__revision_number=1, format="npz_mask")
        new = AnnotationPayload.objects.get(revision__revision_number=2, format="npz_mask")
        self.assertNotEqual(old.file_id, new.file_id, "revision 1 must still point at its own bytes")

    def test_a_frame_with_no_pixels_is_a_deletion(self):
        self._save([frame(0, {self.liver: rect(0, 0, 8, 8)}), frame(500, {self.liver: rect(0, 0, 4, 4)})])
        self._save([frame(0, {self.liver: rect(0, 0, 0, 0)})])
        self.assertEqual(set(self._state()["sources"][str(self.video.pk)]["frames"]), {"500"})
        self.assertEqual(self._read(0).status_code, 404)
        self.assertEqual(self._read(500).status_code, 200)

    def test_deleting_the_last_frame_empties_the_source(self):
        self._save([frame(0, {self.liver: rect(0, 0, 8, 8)})])
        self._save([frame(0, {})])
        self.assertEqual(self._state()["sources"], {})

    def test_saving_identical_bytes_again_reuses_the_stored_file(self):
        mask = rect(0, 0, 8, 8)
        self._save([frame(0, {self.liver: mask})])
        files = self._mask_rows().count()
        objects = len(get_object_storage().objects)
        self._save([frame(0, {self.liver: mask})])
        self.assertEqual(self._mask_rows().count(), files)
        self.assertEqual(len(get_object_storage().objects), objects)
        self.assertEqual(self._state()["revision"], 2)

    # -- refusals leave nothing behind --------------------------------------

    def test_a_stale_revision_is_a_409_and_writes_nothing(self):
        self._save([frame(0, {self.liver: rect(0, 0, 8, 8)})])
        files, objects = self._mask_rows().count(), len(get_object_storage().objects)
        response = self._save([frame(7, {self.liver: rect(0, 0, 3, 3)})], expected=0)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["conflict"])
        self.assertEqual(self._mask_rows().count(), files)
        self.assertEqual(len(get_object_storage().objects), objects)

    def test_an_unknown_label_is_a_400_and_writes_nothing(self):
        response = self._save([frame(0, {"l99": rect(0, 0, 8, 8)})])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AnnotationSet.objects.filter(kind="image_segmentation").exists())
        self.assertEqual(self._mask_rows().count(), 0)

    def test_a_failure_after_upload_removes_the_new_objects(self):
        with mock.patch(
            "annotations.services.image_segmentation._write_revision",
            side_effect=RuntimeError("database went away"),
        ):
            with self.assertRaises(RuntimeError):
                self._save([frame(0, {self.liver: rect(0, 0, 8, 8)})])
        self.assertEqual(
            [k for k in get_object_storage().objects if "image_segmentation" in k], []
        )

    def test_a_disabled_method_is_a_403(self):
        self.project.annotation_methods.clear()
        response = self._save([frame(0, {self.liver: rect(0, 0, 8, 8)})])
        self.assertEqual(response.status_code, 403)

    def test_a_viewer_can_read_but_not_write(self):
        ProjectAccess.objects.filter(user=self.user).update(role="viewer")
        self.assertEqual(self.client.get(self.state_url).status_code, 200)
        self.assertEqual(self._save([frame(0, {self.liver: rect(0, 0, 8, 8)})], expected=0).status_code, 403)
        response = self.client.post(
            self.labels_url, json.dumps({"name": "X"}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)

    def test_another_patients_file_is_refused(self):
        response = self._save(
            [frame(0, {self.liver: rect(0, 0, 8, 8)})], file_id=self.foreign_video.pk, expected=0
        )
        self.assertEqual(response.status_code, 403)

    def test_malformed_bodies_are_400s(self):
        for body in ("{", "[]", json.dumps({"fileId": self.video.pk, "frames": "x"})):
            with self.subTest(body=body):
                response = self.client.post(self.save_url, body, content_type="application/json")
                self.assertEqual(response.status_code, 400)

    def test_reading_needs_integer_parameters(self):
        self.assertEqual(self.client.get(self.frame_url, {"fileId": "x", "timeMs": 0}).status_code, 400)
        self.assertEqual(self._read(12345).status_code, 404)

    def test_an_anonymous_request_is_redirected_to_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.state_url).status_code, 302)
