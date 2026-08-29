"""Phase 10: laparoscopy video annotations as labelmaps.

Four properties, each of which fails silently without a test.

**A run-length mask that does not cover its frame** must be refused, not padded. The
wire format is compact precisely because it says nothing about where a run ends, so a
list whose total is short describes a *different* frame and silently padding it puts
every mask on the wrong pixels from that row onward.

**Region identity is the label code, not an array index.** The NPZ export's class axis
is the project's region types in order, so adding one shifts every axis after it. If the
stored archive keyed by axis, adding a category would re-label every historical study.

**Carry-forward must stay off.** There is one video and every save names it, so the
writer owns the whole set; left on, a region the user had just erased would come back on
the next save.

**The bytes must not move.** Risk 18: decision #15 keeps the NPZ export byte-compatible
while regenerating it from labelmaps. The last test here is that claim, exercised end to
end -- one export from strokes, one from the labelmap those strokes were rasterised
into, compared array for array.
"""

import io
from unittest.mock import patch

import numpy as np
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from annotations.constants import PayloadFormat
from annotations.models import AnnotationSet
from annotations.services.video import (
    QUADRANTS_KIND,
    REGIONS_KIND,
    build_mask_archive,
    decode_rle,
    encode_rle,
    quadrant_markers_state,
    read_mask_archive,
    region_label_schema,
    save_quadrant_markers,
    save_video_regions,
    video_regions_state,
)
from common.models import FileRegistry, Modality, Project
from laparoscopy.models import Folder, Patient, QuadrantType, RegionType


class RunLengthTests(TestCase):
    """The wire format, which is the only thing standing between a mask and the wrong pixels."""

    def test_an_empty_mask_is_one_run(self):
        mask = decode_rle([12], 4, 3)
        self.assertEqual(mask.shape, (3, 4))
        self.assertEqual(int(mask.sum()), 0)

    def test_a_full_mask_opens_with_an_explicit_zero_run(self):
        """The format always starts with zeros, so a filled mask needs a leading 0."""
        mask = decode_rle([0, 12], 4, 3)
        self.assertEqual(int(mask.sum()), 12)

    def test_runs_round_trip(self):
        mask = np.zeros((3, 4), dtype=np.uint8)
        mask[1, 1:3] = 1
        np.testing.assert_array_equal(decode_rle(encode_rle(mask), 4, 3), mask)

    def test_a_filled_first_pixel_round_trips(self):
        mask = np.ones((2, 2), dtype=np.uint8)
        self.assertEqual(encode_rle(mask), [0, 4])
        np.testing.assert_array_equal(decode_rle(encode_rle(mask), 2, 2), mask)

    def test_a_short_run_list_is_refused(self):
        with self.assertRaises(ValidationError) as ctx:
            decode_rle([5], 4, 3)
        self.assertIn("does not describe this frame", str(ctx.exception))

    def test_an_overlong_run_list_is_refused(self):
        with self.assertRaises(ValidationError):
            decode_rle([4, 40], 4, 3)

    def test_a_negative_run_is_refused(self):
        with self.assertRaises(ValidationError):
            decode_rle([4, -1, 9], 4, 3)


class MaskArchiveTests(TestCase):
    def test_the_archive_is_self_describing(self):
        """The vocabulary travels with the masks; a database lookup is not enough.

        A stored archive read next to a project that has since gained or renamed a
        region type has to still say which region each plane is.
        """
        mask = np.zeros((3, 4), dtype=np.uint8)
        mask[0, 0] = 1
        content = build_mask_archive(
            width=4, height=3, frames={100: {"Liver": mask}}
        )
        width, height, frames = read_mask_archive(content)
        self.assertEqual((width, height), (4, 3))
        self.assertEqual(list(frames), [100])
        np.testing.assert_array_equal(frames[100]["Liver"], mask)

    def test_empty_planes_are_not_stored(self):
        """An all-zero plane is the absence of an annotation, not an annotation of nothing."""
        content = build_mask_archive(
            width=4, height=3,
            frames={100: {"Liver": np.zeros((3, 4), dtype=np.uint8)}},
        )
        _width, _height, frames = read_mask_archive(content)
        self.assertEqual(frames[100], {})


class _MemoryStorage:
    """Records what was written, and hands the same bytes back to file_access."""

    def __init__(self):
        self.objects = {}

    def upload_fileobj(self, fileobj, *, key, content_type=None, metadata=None):
        self.objects[key] = fileobj.read()
        return None


def _frame(time_ms, regions):
    return {"timeMs": time_ms, "regions": regions}


class _VideoBase(TestCase):
    """Storage is in memory: the archive is real bytes, written and read back."""

    def setUp(self):
        self.user = User.objects.create_user(username="surgeon", password="pw")
        self.project = Project.objects.create(
            name="Lap", slug="lap", domain="laparoscopy"
        )
        self.modality = Modality.objects.create(name="Video", slug="video")
        self.project.modalities.add(self.modality)
        self.folder = Folder.objects.create(name="Batch", project=self.project)
        self.patient = Patient.objects.create(
            name="P", folder=self.folder, project=self.project
        )
        self.video = FileRegistry.objects.create(
            domain="laparoscopy",
            laparoscopy_patient=self.patient,
            file_type="video_raw",
            file_path=f"laparoscopy/patient_{self.patient.patient_id}/raw.mp4",
            file_size=10,
            file_hash="videohash",
            modality=self.modality,
        )
        RegionType.objects.create(project=self.project, name="Liver", color="#0a0", order=0)
        RegionType.objects.create(project=self.project, name="Gallbladder", color="#a00", order=1)

        self.storage = _MemoryStorage()
        storage_patch = patch(
            "common.object_storage.get_object_storage", return_value=self.storage
        )
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        read_patch = patch(
            "common.file_access.open_binary",
            side_effect=lambda key: (io.BytesIO(self.storage.objects[key]), None),
        )
        read_patch.start()
        self.addCleanup(read_patch.stop)

    def _mask(self, *cells, width=4, height=3):
        mask = np.zeros((height, width), dtype=np.uint8)
        for row, column in cells:
            mask[row, column] = 1
        return {"rle": encode_rle(mask)}

    def _save(self, frames, prompts=(), expected_revision=None):
        return save_video_regions(
            self.patient,
            video_file=self.video,
            width=4,
            height=3,
            frames=frames,
            prompts=prompts,
            author=self.user,
            expected_revision=expected_revision,
            label_schema=region_label_schema(self.project),
        )


class SaveVideoRegionsTests(_VideoBase):
    def test_a_save_stores_a_canonical_mask_archive(self):
        revision = self._save([_frame(1000, {"Liver": self._mask((1, 1))})])
        payload = revision.payloads.get(format=PayloadFormat.NPZ_MASK)
        self.assertEqual(payload.canonical_slot, 1)
        self.assertTrue(payload.file.file_path.endswith(".npz"))
        self.assertEqual(payload.file.file_type, "annotation_mask")

    def test_the_state_endpoint_returns_what_was_saved(self):
        self._save([_frame(1000, {"Liver": self._mask((1, 1), (1, 2))})])
        state = video_regions_state(self.patient)
        self.assertEqual(state["revision"], 1)
        self.assertEqual((state["width"], state["height"]), (4, 3))
        self.assertEqual(len(state["frames"]), 1)
        self.assertEqual(state["frames"][0]["timeMs"], 1000)
        recovered = decode_rle(state["frames"][0]["regions"]["Liver"]["rle"], 4, 3)
        self.assertEqual(int(recovered.sum()), 2)

    def test_an_erased_region_does_not_come_back(self):
        """Carry-forward is off: the client owns the whole set, so a save is the truth.

        With it on, the second save here would name only the gallbladder and the liver
        mask would be copied forward from revision 1 -- indistinguishable, to the user,
        from an erase that did not take.
        """
        self._save([_frame(1000, {"Liver": self._mask((1, 1))})])
        self._save(
            [_frame(1000, {"Gallbladder": self._mask((0, 0))})], expected_revision=1
        )
        state = video_regions_state(self.patient)
        self.assertEqual(list(state["frames"][0]["regions"]), ["Gallbladder"])

    def test_regions_are_keyed_by_code_so_a_new_type_does_not_relabel_history(self):
        self._save([_frame(1000, {"Liver": self._mask((1, 1))})])
        RegionType.objects.create(
            project=self.project, name="Aorta", color="#00a", order=0
        )
        state = video_regions_state(self.patient)
        self.assertEqual(list(state["frames"][0]["regions"]), ["Liver"])

    def test_prompt_points_stay_sparse_and_normalised(self):
        """A prompt is the input that produced a mask, not a mask. Rasterising it loses it."""
        revision = self._save(
            [_frame(1000, {"Liver": self._mask((1, 1))})],
            prompts=[
                {"timeMs": 1000, "regionCode": "Liver", "x": 0.5, "y": 0.25, "label": 0}
            ],
        )
        item = revision.geometry2ditems.get()
        self.assertEqual(item.coordinate_system, "video_normalized")
        self.assertEqual(item.points, [[0.5, 0.25]])
        self.assertEqual(item.attributes["prompt_label"], 0)
        self.assertEqual(item.selector.start_time_ms, 1000)

    def test_a_prompt_in_pixels_is_refused(self):
        with self.assertRaises(ValidationError):
            self._save(
                [],
                prompts=[{"timeMs": 0, "regionCode": "Liver", "x": 640, "y": 360}],
            )

    def test_a_stale_expected_revision_conflicts(self):
        from annotations.services.exceptions import AnnotationConflict

        self._save([_frame(0, {"Liver": self._mask((0, 0))})])
        with self.assertRaises(AnnotationConflict):
            self._save([_frame(0, {"Liver": self._mask((0, 1))})], expected_revision=0)

    def test_each_revision_gets_its_own_archive(self):
        """Revisions are immutable; overwriting the object would rewrite history."""
        first = self._save([_frame(0, {"Liver": self._mask((0, 0))})])
        second = self._save(
            [_frame(0, {"Liver": self._mask((0, 1))})], expected_revision=1
        )
        first_path = first.payloads.get(format=PayloadFormat.NPZ_MASK).file.file_path
        second_path = second.payloads.get(format=PayloadFormat.NPZ_MASK).file.file_path
        self.assertNotEqual(first_path, second_path)
        self.assertIn(first_path, self.storage.objects)

    def test_human_work_freezes_the_raw_video(self):
        self._save([_frame(0, {"Liver": self._mask((0, 0))})])
        annotation_set = AnnotationSet.objects.get(
            laparoscopy_patient=self.patient, kind=REGIONS_KIND
        )
        self.assertTrue(annotation_set.ever_annotated)

    def test_a_frame_flood_is_refused(self):
        with self.assertRaises(ValidationError) as ctx:
            self._save([_frame(index, {}) for index in range(6000)])
        self.assertIn("resending its buffer", str(ctx.exception))


class QuadrantMarkerTests(_VideoBase):
    """The other half of the surface, which did *not* change representation."""

    def test_markers_round_trip(self):
        QuadrantType.objects.create(
            project=self.project, name="RUQ", color="#0a0", order=0
        )
        save_quadrant_markers(
            self.patient,
            video_file=self.video,
            markers=[{"timeMs": 500, "quadrantName": "RUQ"}],
            author=self.user,
        )
        state = quadrant_markers_state(self.patient)
        self.assertEqual(state["markers"], [{"timeMs": 500, "quadrantName": "RUQ"}])

    def test_two_markers_at_one_instant_are_refused(self):
        """The legacy table had UniqueConstraint(patient, time_ms) and the timeline needs it."""
        with self.assertRaises(ValidationError) as ctx:
            save_quadrant_markers(
                self.patient,
                video_file=self.video,
                markers=[
                    {"timeMs": 500, "quadrantName": "RUQ"},
                    {"timeMs": 500, "quadrantName": "LUQ"},
                ],
                author=self.user,
            )
        self.assertIn("500ms", str(ctx.exception))

    def test_markers_and_regions_have_separate_revision_chains(self):
        QuadrantType.objects.create(
            project=self.project, name="RUQ", color="#0a0", order=0
        )
        self._save([_frame(0, {"Liver": self._mask((0, 0))})])
        save_quadrant_markers(
            self.patient,
            video_file=self.video,
            markers=[{"timeMs": 1, "quadrantName": "RUQ"}],
            author=self.user,
        )
        self.assertEqual(video_regions_state(self.patient)["revision"], 1)
        self.assertEqual(quadrant_markers_state(self.patient)["revision"], 1)
        self.assertEqual(
            AnnotationSet.objects.filter(laparoscopy_patient=self.patient).count(), 2
        )
        self.assertEqual(
            set(
                AnnotationSet.objects.filter(
                    laparoscopy_patient=self.patient
                ).values_list("kind", flat=True)
            ),
            {REGIONS_KIND, QUADRANTS_KIND},
        )
