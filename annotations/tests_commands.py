"""The Phase 2 conversion commands.

The properties worth testing here are the operational ones -- the ones that
decide whether a production run is safe to start and safe to repeat:

* **idempotence.** Running twice converts once. Without it, "re-run after fixing
  one patient" duplicates everything that already worked.
* **the machine-output rule survives the conversion.** A pipeline classification
  and an ``auto`` panoramic arch must not set ``ever_annotated``, or the
  conversion itself would lock cases that were never annotated by a person --
  and the lock is monotonic, so that is not undoable.
* **failures are loud.** A row that will not convert stops the run by default.
  A conversion that quietly skips is indistinguishable from one that worked.
* **the crosscheck actually fails.** A gate that cannot report a problem is not
  a gate, and this one stands in front of dropping the legacy tables.
"""

import json
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from annotations.models import (
    AnnotationRevision,
    AnnotationSet,
    Geometry2DItem,
    SpatialAnnotation3DItem,
)
from common.annotation_lock import raw_data_is_locked
from common.models import FileRegistry, Job, Modality, Project
from laparoscopy.models import (
    Folder as LaparoFolder,
    Patient as LaparoPatient,
    QuadrantClassificationMarker,
    QuadrantType,
    RegionAnnotation,
    RegionType,
)
from maxillo.models import (
    Classification,
    Folder,
    IntraoralToothSegmentation,
    PanoramicState,
    Patient,
    VoiceCaption,
)


def _run(command, **kwargs):
    out, err = StringIO(), StringIO()
    call_command(command, stdout=out, stderr=err, **kwargs)
    return out.getvalue() + err.getvalue()


class ConversionTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project, _ = Project.objects.update_or_create(
            slug="maxillo", defaults={"name": "maxillo", "domain": "maxillo"}
        )
        cls.user = User.objects.create_user(username="convert-user", password="x")
        cls.folder = Folder.objects.create(name="Convert", project=cls.project)
        cls.patient = Patient.objects.create(
            name="Converted", folder=cls.folder, project=cls.project
        )
        cls.modality = Modality.objects.create(name="Convert CBCT", slug="cbct")

    def _file(self, path, file_type="cbct_processed", **extra):
        return FileRegistry.objects.create(
            file_type=file_type,
            file_path=path,
            file_size=1,
            file_hash=extra.pop("file_hash", "0" * 64),
            domain="maxillo",
            patient=self.patient,
            **extra,
        )


class ClassificationConversionTests(ConversionTestCase):
    def test_a_manual_classification_becomes_five_events_and_locks_the_case(self):
        Classification.objects.create(
            patient=self.patient,
            classifier="manual",
            sagittal_left="I",
            sagittal_right="II",
            vertical="Normal",
            transverse="Unknown",
            midline="Unknown",
        )

        _run("annotations_convert_legacy", surface=["classification"])

        annotation_set = AnnotationSet.objects.get(
            patient=self.patient, kind="occlusion_classification"
        )
        self.assertTrue(annotation_set.ever_annotated)
        self.assertEqual(
            annotation_set.revisions.get().eventannotationitems.count(), 5
        )
        self.assertTrue(raw_data_is_locked(self.patient))

    def test_a_pipeline_classification_converts_without_locking(self):
        """Machine output has never locked a case; the conversion must not either."""
        Classification.objects.create(
            patient=self.patient,
            classifier="pipeline",
            sagittal_left="I",
            sagittal_right="I",
            vertical="Normal",
            transverse="Normal",
            midline="Normal",
        )

        _run("annotations_convert_legacy", surface=["classification"])

        annotation_set = AnnotationSet.objects.get(
            patient=self.patient, kind="occlusion_classification"
        )
        self.assertFalse(annotation_set.ever_annotated)

    def test_running_twice_converts_once(self):
        Classification.objects.create(
            patient=self.patient, classifier="manual", vertical="Normal"
        )

        _run("annotations_convert_legacy", surface=["classification"])
        output = _run("annotations_convert_legacy", surface=["classification"])

        self.assertEqual(AnnotationRevision.objects.count(), 1)
        self.assertIn("skipped 1", output)

    def test_a_dry_run_writes_nothing(self):
        Classification.objects.create(
            patient=self.patient, classifier="manual", vertical="Normal"
        )

        output = _run(
            "annotations_convert_legacy", surface=["classification"], dry_run=True
        )

        self.assertIn("dry run", output)
        self.assertFalse(AnnotationSet.objects.exists())


class IntraoralConversionTests(ConversionTestCase):
    def test_polygons_convert_with_their_fdi_labels(self):
        image = self._file("maxillo/intraoral/photo.png", file_type="intraoral_processed")
        IntraoralToothSegmentation.objects.create(
            patient=self.patient,
            image_file=image,
            teeth={"11": [[[0, 0], [10, 0], [10, 10]]]},
        )

        _run("annotations_convert_legacy", surface=["intraoral"])

        item = Geometry2DItem.objects.get()
        self.assertEqual(item.label.code, "11")
        self.assertEqual(item.label.schema.slug, "fdi-permanent")
        self.assertEqual(item.points, [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]])

    def test_an_empty_segmentation_row_is_skipped_not_converted(self):
        """It records the tool being opened, not a segmentation."""
        image = self._file("maxillo/intraoral/blank.png", file_type="intraoral_processed")
        IntraoralToothSegmentation.objects.create(
            patient=self.patient, image_file=image, teeth={}
        )

        output = _run("annotations_convert_legacy", surface=["intraoral"])

        self.assertIn("converted 0", output)
        self.assertFalse(AnnotationSet.objects.exists())

    def test_two_images_share_one_set_and_the_primary_slot_does_not_move(self):
        first = self._file("maxillo/intraoral/a.png", file_type="intraoral_processed")
        second = self._file("maxillo/intraoral/b.png", file_type="intraoral_processed")
        for image in (first, second):
            IntraoralToothSegmentation.objects.create(
                patient=self.patient,
                image_file=image,
                teeth={"11": [[[0, 0], [1, 0], [1, 1]]]},
            )

        _run("annotations_convert_legacy", surface=["intraoral"])

        annotation_set = AnnotationSet.objects.get(kind="intraoral_segmentation")
        self.assertEqual(annotation_set.targets.count(), 2)
        self.assertEqual(annotation_set.targets.filter(primary_slot=1).count(), 1)

    def test_an_unknown_fdi_code_stops_the_run(self):
        """Writing it unlabelled would export it under the wrong segment."""
        image = self._file("maxillo/intraoral/odd.png", file_type="intraoral_processed")
        IntraoralToothSegmentation.objects.create(
            patient=self.patient,
            image_file=image,
            teeth={"99": [[[0, 0], [1, 0], [1, 1]]]},
        )

        with self.assertRaises(CommandError):
            _run("annotations_convert_legacy", surface=["intraoral"])

    def test_continue_on_error_reports_and_carries_on(self):
        good_image = self._file("maxillo/intraoral/good.png", file_type="intraoral_processed")
        bad_image = self._file("maxillo/intraoral/bad.png", file_type="intraoral_processed")
        IntraoralToothSegmentation.objects.create(
            patient=self.patient,
            image_file=bad_image,
            teeth={"99": [[[0, 0], [1, 0], [1, 1]]]},
        )
        IntraoralToothSegmentation.objects.create(
            patient=self.patient,
            image_file=good_image,
            teeth={"11": [[[0, 0], [1, 0], [1, 1]]]},
        )

        with self.assertRaises(CommandError):
            # Still non-zero at the end: carrying on is not the same as passing.
            _run(
                "annotations_convert_legacy",
                surface=["intraoral"],
                continue_on_error=True,
            )

        self.assertEqual(Geometry2DItem.objects.count(), 1)


class PanoramicConversionTests(ConversionTestCase):
    def _state(self, geometry_source):
        source = self._file(f"maxillo/processed/{geometry_source}.nii.gz")
        return PanoramicState.objects.create(
            patient=self.patient,
            source_file=source,
            source_file_key="primary",
            source_file_hash="1" * 64,
            mip_file=self._file(f"maxillo/pano/{geometry_source}-mip.png"),
            raysum_file=self._file(f"maxillo/pano/{geometry_source}-raysum.png"),
            axial_slice=128,
            volume_shape=[400, 400, 300],
            spline=[[0, 0], [10, 5], [20, 5], [30, 0]],
            geometry_source=geometry_source,
            default_mode="mip",
            request_hash="2" * 64,
        )

    def test_an_edited_arch_converts_and_locks(self):
        self._state("custom_cp")

        _run("annotations_convert_legacy", surface=["panoramic"])

        annotation_set = AnnotationSet.objects.get(kind="panoramic_arch")
        self.assertTrue(annotation_set.ever_annotated)
        item = Geometry2DItem.objects.get()
        self.assertEqual(item.selector.slice_index, 128)
        self.assertEqual(item.selector.slice_axis, "axial")

    def test_an_auto_arch_converts_without_locking(self):
        """It explains the baked strips; nobody drew it."""
        self._state("auto")

        _run("annotations_convert_legacy", surface=["panoramic"])

        annotation_set = AnnotationSet.objects.get(kind="panoramic_arch")
        self.assertFalse(annotation_set.ever_annotated)

    def test_the_source_volume_becomes_the_target_with_its_hash(self):
        self._state("custom_cp")

        _run("annotations_convert_legacy", surface=["panoramic"])

        target = AnnotationSet.objects.get(kind="panoramic_arch").targets.get()
        self.assertEqual(target.source_resource.content_hash, "1" * 64)
        self.assertEqual(target.source_resource.kind, "logical_volume")


class LaparoscopyConversionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.create(
            name="convert-laparo", slug="convert-laparo", domain="laparoscopy"
        )
        cls.folder = LaparoFolder.objects.create(name="F", project=cls.project)
        cls.patient = LaparoPatient.objects.create(
            project=cls.project, folder=cls.folder
        )
        cls.video = FileRegistry.objects.create(
            file_type="video_raw",
            file_path="laparoscopy/raw/case.mp4",
            file_size=1,
            file_hash="3" * 64,
            domain="laparoscopy",
            laparoscopy_patient=cls.patient,
        )
        cls.region_type = RegionType.objects.create(project=cls.project, name="Liver")
        cls.quadrant_type = QuadrantType.objects.create(project=cls.project, name="RUQ")

    def test_a_region_stroke_converts_with_milliseconds_and_a_project_schema(self):
        RegionAnnotation.objects.create(
            patient=self.patient,
            region_type=self.region_type,
            tool="brush",
            frame_time=2 / 30,
            points=[0, 0, 10, 10, 20, 0],
            stroke_width=4.0,
        )

        _run("annotations_convert_legacy", surface=["video_regions"], domain=["laparoscopy"])

        item = Geometry2DItem.objects.get()
        self.assertEqual(item.selector.start_time_ms, 67)
        self.assertEqual(item.label.code, "Liver")
        self.assertEqual(item.label.schema.slug, f"laparoscopy-regions-project-{self.project.pk}")
        self.assertEqual(item.stroke_width, 4.0)

    def test_a_region_on_a_patient_with_no_video_stops_the_run(self):
        """Strokes in frame pixels need the frame; anchoring them to nothing
        would leave coordinates with no resource behind them."""
        other = LaparoPatient.objects.create(project=self.project, folder=self.folder)
        RegionAnnotation.objects.create(
            patient=other,
            region_type=self.region_type,
            tool="brush",
            frame_time=1.0,
            points=[0, 0, 1, 1],
        )

        with self.assertRaises(CommandError):
            _run(
                "annotations_convert_legacy",
                surface=["video_regions"],
                domain=["laparoscopy"],
            )

    def test_a_quadrant_marker_keeps_its_integer_timestamp(self):
        QuadrantClassificationMarker.objects.create(
            patient=self.patient, quadrant_type=self.quadrant_type, time_ms=4200
        )

        _run("annotations_convert_legacy", surface=["quadrants"], domain=["laparoscopy"])

        event = AnnotationSet.objects.get(kind="video_quadrants").revisions.get().eventannotationitems.get()
        self.assertEqual(event.time_ms, 4200)
        self.assertEqual(event.value, "RUQ")

    def test_laparoscopy_notes_do_not_become_an_occlusion_classification(self):
        """The two domains share a table name and nothing else."""
        from laparoscopy.models import Classification as LaparoClassification

        LaparoClassification.objects.create(
            patient=self.patient, classifier="manual", notes="Adhesions noted"
        )

        _run(
            "annotations_convert_legacy",
            surface=["classification"],
            domain=["laparoscopy"],
        )

        annotation_set = AnnotationSet.objects.get(laparoscopy_patient=self.patient)
        self.assertEqual(annotation_set.kind, "study_notes")
        self.assertEqual(
            annotation_set.revisions.get().eventannotationitems.get().value,
            "Adhesions noted",
        )


class VoiceCaptionConversionTests(ConversionTestCase):
    def test_a_caption_converts_on_every_domain(self):
        VoiceCaption.objects.create(
            patient=self.patient,
            user=self.user,
            duration=4.5,
            text_caption="Impacted third molar",
        )

        _run("annotations_convert_legacy", surface=["voice_captions"])

        event = AnnotationSet.objects.get(kind="voice_caption").revisions.get().eventannotationitems.get()
        self.assertEqual(event.value, "Impacted third molar")
        self.assertEqual(event.attributes["duration_seconds"], 4.5)

    def test_a_caption_with_no_transcript_still_converts(self):
        VoiceCaption.objects.create(
            patient=self.patient, user=self.user, duration=2.0, text_caption=""
        )

        _run("annotations_convert_legacy", surface=["voice_captions"])

        event = AnnotationSet.objects.get(kind="voice_caption").revisions.get().eventannotationitems.get()
        self.assertFalse(event.attributes["has_transcript"])
        self.assertIsNone(event.target_id)


class MaterializeLandmarkTests(ConversionTestCase):
    def _landmark_file(self, document, path="maxillo/processed/ios/l.json"):
        row = self._file(path, file_type="ios_landmarks")
        self._documents[row.file_path] = json.dumps(document).encode("utf-8")
        return row

    def setUp(self):
        self._documents = {}
        patcher = mock.patch(
            "annotations.management.commands.annotations_materialize_landmarks.open_binary"
        )
        self.open_binary = patcher.start()
        self.addCleanup(patcher.stop)

        def fake_open(path):
            import io

            return io.BytesIO(self._documents[path]), None

        self.open_binary.side_effect = fake_open

    def test_landmarks_land_in_resource_local_with_their_fdi_labels(self):
        key = f"{self.patient.patient_id}_upper_FDI_11"
        self._landmark_file({key: {"incisal": [1.0, 2.0, 3.0]}})

        _run("annotations_materialize_landmarks")

        item = SpatialAnnotation3DItem.objects.get()
        self.assertEqual(item.coordinate_system, "resource_local")
        self.assertEqual(item.points, [[1.0, 2.0, 3.0]])
        self.assertEqual(item.label.code, "11")
        self.assertEqual(item.frame_of_reference_uid, "")

    def test_running_twice_converts_once(self):
        key = f"{self.patient.patient_id}_upper_FDI_11"
        self._landmark_file({key: {"incisal": [1, 2, 3]}})

        _run("annotations_materialize_landmarks")
        output = _run("annotations_materialize_landmarks")

        self.assertEqual(SpatialAnnotation3DItem.objects.count(), 1)
        self.assertIn("skipped 1", output)

    def test_a_prediction_row_is_never_materialized(self):
        """It is model output; converting it would look like somebody's work."""
        self._file("maxillo/processed/ios/pred.json", file_type="ios_landmarks_prediction")

        output = _run("annotations_materialize_landmarks")

        self.assertIn("converted 0", output)
        self.assertFalse(AnnotationSet.objects.exists())

    def test_a_malformed_document_stops_the_run(self):
        row = self._file("maxillo/processed/ios/bad.json", file_type="ios_landmarks")
        self._documents[row.file_path] = b"{ not json"

        with self.assertRaises(CommandError):
            _run("annotations_materialize_landmarks")

    def test_an_oversized_document_is_refused_before_it_is_parsed(self):
        row = self._file("maxillo/processed/ios/huge.json", file_type="ios_landmarks")
        self._documents[row.file_path] = b"[" + b"0," * 5_000_000

        with self.assertRaises(CommandError):
            _run("annotations_materialize_landmarks")

    def test_a_dry_run_reads_but_writes_nothing(self):
        key = f"{self.patient.patient_id}_upper_FDI_11"
        self._landmark_file({key: {"incisal": [1, 2, 3]}})

        _run("annotations_materialize_landmarks", dry_run=True)

        self.assertFalse(SpatialAnnotation3DItem.objects.exists())


class CrosscheckTests(ConversionTestCase):
    def test_an_unconverted_row_fails_the_check(self):
        Classification.objects.create(
            patient=self.patient, classifier="manual", vertical="Normal"
        )

        with self.assertRaises(CommandError):
            _run("annotations_crosscheck")

    def test_the_check_passes_once_the_conversion_has_run(self):
        Classification.objects.create(
            patient=self.patient, classifier="manual", vertical="Normal"
        )
        _run("annotations_convert_legacy", surface=["classification"])

        output = _run("annotations_crosscheck")

        self.assertIn("0 problem(s)", output)

    def test_an_empty_database_passes(self):
        self.assertIn("0 problem(s)", _run("annotations_crosscheck"))

    def test_it_names_the_row_it_could_not_find(self):
        row = Classification.objects.create(
            patient=self.patient, classifier="manual", vertical="Normal"
        )

        with self.assertRaises(CommandError):
            output = _run("annotations_crosscheck")
            self.assertIn(f"legacy:maxillo.classification:{row.pk}", output)

    def test_it_reports_bytes_that_changed_under_an_annotation(self):
        """The failure the raw-data lock exists to prevent, found after the fact."""
        source = self._file("maxillo/processed/drift.nii.gz")
        PanoramicState.objects.create(
            patient=self.patient,
            source_file=source,
            source_file_key="primary",
            source_file_hash="1" * 64,
            mip_file=self._file("maxillo/pano/drift-mip.png"),
            raysum_file=self._file("maxillo/pano/drift-raysum.png"),
            axial_slice=1,
            volume_shape=[10, 10, 10],
            spline=[[0, 0], [1, 1], [2, 2], [3, 3]],
            geometry_source="custom_cp",
            default_mode="mip",
            request_hash="2" * 64,
        )
        _run("annotations_convert_legacy", surface=["panoramic"])

        # Somebody rewrote the volume's affine and restamped the hash.
        target = AnnotationSet.objects.get(kind="panoramic_arch").targets.get()
        resource = target.source_resource
        resource.content_hash = "9" * 64
        resource.save(update_fields=["content_hash"])

        with self.assertRaises(CommandError):
            _run("annotations_crosscheck")

    def test_an_empty_set_is_reported_but_does_not_fail(self):
        annotation_set = AnnotationSet(kind="measurements")
        annotation_set.set_patient(self.patient)
        annotation_set.save()

        output = _run("annotations_crosscheck")

        self.assertIn("no revisions", output)
        self.assertIn("0 problem(s)", output)
