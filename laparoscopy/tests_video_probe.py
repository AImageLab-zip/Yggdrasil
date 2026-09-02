"""Where a video's ffprobe result comes from, and what happens when it does not.

The annotator refuses to mount for a video with no recorded probe -- correctly, since
a browser cannot read a frame rate and guessing 30 for a 25 fps recording mis-files
every mask while looking right. What was missing was anything that *recorded* one:
``probe_and_record`` had a single caller, a migration command that visits only
patients carrying legacy strokes and writes onto the ``video_raw`` row. These tests
pin the two ends of that gap -- the upload path, and the backfill.

``probe_video`` itself is patched throughout. It shells out to ``ffprobe`` and is
already exercised by the export's own tests; what is under test here is *who calls it,
with what, and what happens when it raises*.
"""

from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile, TemporaryUploadedFile
from django.core.management import call_command
from django.test import TestCase

from common.models import FileRegistry, Modality, Project
from laparoscopy import video_probe
from laparoscopy.models import Folder, Patient

PROBE = {"width": 1920, "height": 1080, "fps": 25.0, "frame_count": 500}


class RecordingAProbeFromAnUploadTests(TestCase):
    def setUp(self):
        self.row = FileRegistry.objects.create(
            domain="laparoscopy",
            file_type="video_raw",
            file_path="laparoscopy/op.mp4",
            file_size=10,
            file_hash="h",
            metadata={"original_filename": "op.mp4"},
        )

    def test_a_spooled_upload_is_probed_where_it_already_is(self):
        """The usual path: a surgical recording is always over the memory threshold."""
        upload = TemporaryUploadedFile("op.mp4", "video/mp4", 10, None)
        upload.write(b"0123456789")
        upload.flush()

        with mock.patch.object(
            video_probe, "probe_video", return_value=dict(PROBE)
        ) as probe_video:
            result = video_probe.probe_and_record_upload(self.row, upload)

        self.assertEqual(result, PROBE)
        # Probed in place -- no second copy of a multi-gigabyte file.
        probe_video.assert_called_once_with(upload.temporary_file_path())
        self.row.refresh_from_db()
        self.assertEqual(self.row.metadata["probe"], PROBE)

    def test_an_in_memory_upload_is_written_out_rather_than_refused(self):
        """Being small is not a reason to know less about a file."""
        upload = SimpleUploadedFile("op.mp4", b"0123456789", content_type="video/mp4")

        seen = {}

        def fake_probe(path):
            with open(path, "rb") as handle:
                seen["bytes"] = handle.read()
            return dict(PROBE)

        with mock.patch.object(video_probe, "probe_video", side_effect=fake_probe):
            result = video_probe.probe_and_record_upload(self.row, upload)

        self.assertEqual(result, PROBE)
        self.assertEqual(seen["bytes"], b"0123456789")
        self.row.refresh_from_db()
        self.assertEqual(self.row.metadata["probe"], PROBE)

    def test_a_failing_probe_does_not_fail_the_upload(self):
        """The file is stored and playable; what is lost is the annotator, not the video."""
        upload = SimpleUploadedFile("op.mp4", b"junk", content_type="video/mp4")

        with mock.patch.object(
            video_probe, "probe_video", side_effect=RuntimeError("ffprobe failed")
        ):
            with self.assertLogs("laparoscopy.video_probe", level="ERROR"):
                result = video_probe.probe_and_record_upload(self.row, upload)

        self.assertIsNone(result)
        self.row.refresh_from_db()
        self.assertNotIn("probe", self.row.metadata)


class UploadPathRecordsAProbeTests(TestCase):
    """`save_video_to_dataset` is the caller `video_probe` was written for.

    Its docstring said the upload path recorded a probe. It did not, which is why a
    video uploaded after Phase 10 could never mount the annotator.
    """

    def setUp(self):
        self.project, _ = Project.objects.update_or_create(
            slug="laparoscopy", defaults={"name": "Laparoscopy", "domain": "laparoscopy"}
        )
        Modality.objects.get_or_create(
            slug="video", defaults={"name": "Video", "domain": "laparoscopy"}
        )
        folder = Folder.objects.create(name="Cases", project=self.project)
        self.patient = Patient.objects.create(
            name="Case", folder=folder, project=self.project
        )

    @mock.patch("common.signals.celery_app.send_task")
    def test_saving_a_video_records_its_probe(self, _send_task):
        from laparoscopy import file_utils

        upload = SimpleUploadedFile("op.mp4", b"0123456789", content_type="video/mp4")

        with mock.patch.object(
            file_utils, "upload_uploaded_file_to_storage",
            return_value=("laparoscopy/op.mp4", 10, "h"),
        ), mock.patch.object(video_probe, "probe_video", return_value=dict(PROBE)):
            row, _job = file_utils.save_video_to_dataset(self.patient, upload)

        self.assertIsNotNone(row)
        self.assertEqual(video_probe.recorded_probe(row), PROBE)


class BackfillCommandTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.update_or_create(
            slug="laparoscopy", defaults={"name": "Laparoscopy", "domain": "laparoscopy"}
        )
        folder = Folder.objects.create(name="Cases", project=self.project)
        self.patient = Patient.objects.create(
            name="Case", folder=folder, project=self.project
        )

    def _row(self, file_type, subtype="", metadata=None):
        return FileRegistry.objects.create(
            domain="laparoscopy",
            laparoscopy_patient=self.patient,
            file_type=file_type,
            subtype=subtype,
            file_path=f"laparoscopy/{file_type}{subtype}.mp4",
            file_size=10,
            file_hash=f"h{file_type}{subtype}",
            metadata=metadata or {},
        )

    def _run(self, probe=None, **options):
        """Run the command with object storage stubbed out."""
        import contextlib

        @contextlib.contextmanager
        def fake_download(key, suffix=""):
            yield f"(not-a-real-path)/{key}"

        with mock.patch(
            "common.object_storage.download_to_tempfile", fake_download
        ), mock.patch.object(
            video_probe, "probe_video", side_effect=probe or (lambda path: dict(PROBE))
        ):
            call_command("laparoscopy_probe_videos", **options)

    def test_it_probes_every_video_row_not_only_the_raw_one(self):
        """The page plays whichever row ranks highest, so every row must be answerable."""
        raw = self._row("video_raw")
        compressed = self._row("video_processed", "compressed")

        self._run()

        for row in (raw, compressed):
            row.refresh_from_db()
            self.assertEqual(video_probe.recorded_probe(row), PROBE, row.file_type)

    def test_it_reaches_a_patient_with_no_legacy_annotations(self):
        """`annotations_rasterize_video_masks` cannot: it iterates stroke rows.

        This patient has a video and has never been annotated, which is the shape that
        could never be repaired before.
        """
        row = self._row("video_raw")
        self.assertFalse(self.patient.region_annotations.exists())

        self._run()

        row.refresh_from_db()
        self.assertIsNotNone(video_probe.recorded_probe(row))

    def test_an_already_probed_row_is_skipped(self):
        row = self._row("video_raw", metadata={"probe": dict(PROBE)})

        with mock.patch.object(video_probe, "probe_video") as probe_video:
            call_command("laparoscopy_probe_videos")

        probe_video.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(video_probe.recorded_probe(row), PROBE)

    def test_dry_run_writes_nothing(self):
        row = self._row("video_raw")

        with mock.patch.object(video_probe, "probe_video") as probe_video:
            call_command("laparoscopy_probe_videos", dry_run=True)

        probe_video.assert_not_called()
        row.refresh_from_db()
        self.assertIsNone(video_probe.recorded_probe(row))

    def test_one_bad_file_does_not_stop_the_sweep(self):
        bad = self._row("video_raw")
        good = self._row("video_processed", "compressed")

        def probe(path):
            if "video_raw" in path:
                raise RuntimeError("ffprobe failed")
            return dict(PROBE)

        self._run(probe=probe)

        bad.refresh_from_db()
        good.refresh_from_db()
        self.assertIsNone(video_probe.recorded_probe(bad))
        self.assertIsNotNone(video_probe.recorded_probe(good))

    def test_it_reports_when_a_patient_s_videos_disagree_on_frame_size(self):
        """No probe reconciles this, so it is named rather than papered over.

        A mask stored as (height, width) against the raw file does not describe the
        compressed file the page plays.
        """
        self._row("video_raw")
        self._row("video_processed", "compressed")

        def probe(path):
            if "video_raw" in path:
                return dict(PROBE)
            return {**PROBE, "width": 1280, "height": 720}

        from io import StringIO

        errors = StringIO()
        import contextlib

        @contextlib.contextmanager
        def fake_download(key, suffix=""):
            yield f"(not-a-real-path)/{key}"

        with mock.patch(
            "common.object_storage.download_to_tempfile", fake_download
        ), mock.patch.object(video_probe, "probe_video", side_effect=probe):
            call_command("laparoscopy_probe_videos", stderr=errors)

        message = errors.getvalue()
        self.assertIn("disagree on frame size", message)
        self.assertIn("1280x720", message)
        self.assertIn("1920x1080", message)
