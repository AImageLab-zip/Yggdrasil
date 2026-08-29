"""The panoramic arch through the annotations app.

What is specific to this surface, and therefore what is worth testing here rather than
leaning on ``tests_multi_target.py``:

- **An ``auto`` arch is machine output.** ``panoramic_warmup`` generates one for every
  patient in a folder without anybody looking, so filing it as human work would freeze
  the raw data of a whole cohort -- monotonically, per decision #18.
- **One conversion, two callers.** ``annotations_convert_legacy`` and the live editor go
  through the same adapter and anchor under the same role, or a converted study read back
  after its first live edit holds two arches.
- **A save replaces the arch rather than carrying one forward.** A patient has exactly one
  arch; the carry-forward that is right for a photo stack would copy a superseded arch
  onto the revision that replaced it.
- **A replaced source is detected without deleting anything**, from the revision's
  ``source_fingerprint``.
"""

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from annotations.adapters import legacy_maxillo
from annotations.adapters.panoramic import (
    PANORAMIC_KIND,
    arch_from_items,
    panoramic_arch,
)
from annotations.constants import (
    AnnotationOrigin,
    CoordinateSystem,
    Geometry2DType,
    PayloadFormat,
    SliceAxis,
)
from annotations.models import AnnotationSet, Geometry2DItem
from annotations.services.panoramic import (
    SEGMENTATION_ROLE,
    VOLUME_ROLE,
    arch_describes_source,
    arch_origin,
    panoramic_arch_state,
    save_panoramic_arch,
)
from common.models import FileRegistry, Project
from maxillo.models import Folder, Patient

SPLINE = [[10, 20], [25, 30], [50, 40], [75, 30]]


class PanoramicAdapterTests(SimpleTestCase):
    """Pure translation: legacy values in, descriptors out. No database."""

    def test_the_arch_is_an_open_polyline_in_one_axial_slice(self):
        descriptors = panoramic_arch(
            SPLINE,
            axial_slice=20,
            volume_shape=[100, 80, 40],
            geometry_source="auto",
            default_mode="mip",
            algorithm_version="panorex-js-v2-mip",
        )

        self.assertEqual(len(descriptors), 1)
        descriptor = descriptors[0]
        self.assertEqual(descriptor["geometry_type"], Geometry2DType.POLYLINE)
        self.assertEqual(descriptor["coordinate_system"], CoordinateSystem.SLICE_PIXEL)
        self.assertFalse(descriptor["closed"])
        self.assertEqual(descriptor["points"], SPLINE)
        # The index is on the selector, never only in the attributes: a pair of numbers
        # "in the axial slice" is unplaceable without knowing which one.
        self.assertEqual(descriptor["selector"]["slice_index"], 20)
        self.assertEqual(descriptor["selector"]["slice_axis"], SliceAxis.AXIAL)

    def test_either_shape_of_a_stored_spline_is_accepted(self):
        bare = panoramic_arch(
            SPLINE, axial_slice=1, volume_shape=[2, 2, 2],
            geometry_source="auto", default_mode="mip",
        )
        wrapped = panoramic_arch(
            {"controlPoints": SPLINE}, axial_slice=1, volume_shape=[2, 2, 2],
            geometry_source="auto", default_mode="mip",
        )
        self.assertEqual(bare[0]["points"], wrapped[0]["points"])

    def test_an_arch_of_fewer_than_two_points_is_not_an_arch(self):
        with self.assertRaises(ValidationError):
            panoramic_arch(
                [[1, 1]], axial_slice=1, volume_shape=[2, 2, 2],
                geometry_source="auto", default_mode="mip",
            )

    def test_the_legacy_module_delegates_rather_than_reimplementing(self):
        """One conversion for the converter and the live editor.

        Two would drift, and ``annotations_crosscheck`` would report a difference on
        every study anybody had edited -- burying the signal it exists to give.
        """
        kwargs = dict(
            axial_slice=20, volume_shape=[100, 80, 40],
            geometry_source="custom_cp", default_mode="raysum",
            algorithm_version="panorex-js-v2-mip",
        )
        self.assertEqual(
            legacy_maxillo.panoramic_arch(SPLINE, **kwargs),
            panoramic_arch(SPLINE, **kwargs),
        )

    def test_only_an_auto_arch_is_machine_output(self):
        self.assertEqual(arch_origin("auto"), AnnotationOrigin.PREDICTION)
        self.assertEqual(arch_origin("custom_cp"), AnnotationOrigin.MANUAL)


class PanoramicServiceTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Arch", slug="arch-project", domain="maxillo"
        )
        self.folder = Folder.objects.create(name="Arches", project=self.project)
        self.patient = Patient.objects.create(
            name="Arch patient", folder=self.folder, project=self.project
        )
        self.author = User.objects.create_user(username="arch-author", password="x")
        self.volume = self._file("volume_nifti", "volume", "a")
        self.segmentation = self._file("segmentation_nifti", "segmentation", "b")

    def _file(self, subtype, name, hash_char):
        return FileRegistry.objects.create(
            file_type="cbct_processed",
            subtype=subtype,
            file_path=f"maxillo/processed/cbct/{name}.nii.gz",
            file_size=8,
            file_hash=hash_char * 64,
            domain="maxillo",
            patient=self.patient,
        )

    def _strip(self, variant, hash_char):
        row = FileRegistry.objects.create(
            file_type="panoramic_processed",
            subtype=variant,
            file_path=f"maxillo/processed/panoramic/{hash_char}/{variant}.png",
            file_size=12,
            file_hash=hash_char * 64,
            metadata={"generated_from": "browser_cbct_to_panoramic"},
            domain="maxillo",
            patient=self.patient,
        )
        return {"variant": variant, "file_obj": row, "content_hash": row.file_hash, "byte_size": 12}

    def _save(self, *, spline=None, geometry_source="auto", axial_slice=20,
              expected_revision=None, volume=None, segmentation=None, strips=None,
              hash_char="1"):
        return save_panoramic_arch(
            self.patient,
            volume_file=volume or self.volume,
            volume_file_key="primary",
            volume_hash=(volume or self.volume).file_hash,
            segmentation_file=segmentation or self.segmentation,
            segmentation_file_key="primary",
            segmentation_hash=(segmentation or self.segmentation).file_hash,
            spline=spline or SPLINE,
            axial_slice=axial_slice,
            volume_shape=[100, 80, 40],
            geometry_source=geometry_source,
            default_mode="mip",
            algorithm_version="panorex-js-v2-mip",
            strips=strips if strips is not None else [
                self._strip("mip", hash_char), self._strip("raysum", hash_char)
            ],
            author=self.author,
            expected_revision=expected_revision,
        )

    def _source(self, volume=None, segmentation=None):
        volume = volume or self.volume
        segmentation = segmentation or self.segmentation
        return {
            "volume_file": volume,
            "volume_file_key": "primary",
            "volume_hash": volume.file_hash,
            "segmentation_file": segmentation,
            "segmentation_file_key": "primary",
            "segmentation_hash": segmentation.file_hash,
        }

    def test_an_auto_arch_never_sets_the_monotonic_lock_flag(self):
        revision = self._save()

        annotation_set = AnnotationSet.objects.get(kind=PANORAMIC_KIND)
        self.assertEqual(revision.origin, AnnotationOrigin.PREDICTION)
        self.assertFalse(annotation_set.ever_annotated)

    def test_an_edited_arch_does(self):
        self._save(geometry_source="custom_cp")

        self.assertTrue(AnnotationSet.objects.get(kind=PANORAMIC_KIND).ever_annotated)

    def test_an_auto_arch_after_an_edited_one_cannot_thaw_the_case(self):
        self._save(geometry_source="custom_cp")
        self._save(geometry_source="auto", expected_revision=1, hash_char="2")

        self.assertTrue(AnnotationSet.objects.get(kind=PANORAMIC_KIND).ever_annotated)

    def test_the_arch_round_trips_through_the_record(self):
        self._save(spline=SPLINE, axial_slice=17)

        state = panoramic_arch_state(self.patient)
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["arch"]["spline"], SPLINE)
        self.assertEqual(state["arch"]["axial_slice"], 17)
        self.assertEqual(state["arch"]["geometry_source"], "auto")
        self.assertEqual(state["arch"]["default_mode"], "mip")
        self.assertEqual(state["arch"]["volume_shape"], [100, 80, 40])

    def test_both_strips_are_derived_payloads_and_neither_is_canonical(self):
        revision = self._save()

        payloads = {payload.variant: payload for payload in revision.payloads.all()}
        self.assertEqual(set(payloads), {"mip", "raysum"})
        for payload in payloads.values():
            self.assertEqual(payload.format, PayloadFormat.PNG_RENDER)
            self.assertIsNone(payload.canonical_slot)
            self.assertIsNotNone(payload.file)
            self.assertEqual(payload.byte_size, 12)

    def test_an_unknown_strip_variant_is_refused(self):
        with self.assertRaises(ValidationError):
            self._save(strips=[self._strip("mip", "1") | {"variant": "sagittal"}])

    def test_the_volume_and_the_segmentation_are_both_named(self):
        """The arch is drawn on the CBCT and *fitted* against the mask.

        Both belong in the fingerprint, or a re-run segmentation leaves an arch that was
        fitted to bytes nobody has any more, with nothing recording that.
        """
        revision = self._save()

        roles = {
            target.role: target.source_resource.file_id
            for target in revision.annotation_set.targets.select_related("source_resource")
        }
        self.assertEqual(
            roles, {VOLUME_ROLE: self.volume.id, SEGMENTATION_ROLE: self.segmentation.id}
        )
        self.assertEqual(
            set(revision.source_fingerprint.values()),
            {self.volume.file_hash, self.segmentation.file_hash},
        )

    def test_only_the_volume_carries_the_arch(self):
        revision = self._save()

        items = Geometry2DItem.objects.filter(revision=revision)
        self.assertEqual(items.count(), 1)
        self.assertEqual(items.get().target.role, VOLUME_ROLE)

    def test_a_replaced_volume_does_not_leave_two_arches_on_one_revision(self):
        """The defect ``carry_forward=False`` exists to prevent.

        Replacing the CBCT anchors the new arch to a *new* volume resource, leaving the
        old target attached as history. Carried forward, its arch would land on the same
        revision -- and the reader takes the first polyline it finds, so the study would
        come back holding an arch drawn on bytes that no longer exist.
        """
        self._save()
        replacement = self._file("volume_nifti", "replacement", "c")

        revision = self._save(
            volume=replacement, spline=[[1, 1], [2, 2], [3, 3], [4, 4]],
            expected_revision=1, hash_char="2",
        )

        self.assertEqual(Geometry2DItem.objects.filter(revision=revision).count(), 1)
        self.assertEqual(
            panoramic_arch_state(self.patient)["arch"]["spline"],
            [[1, 1], [2, 2], [3, 3], [4, 4]],
        )

    def test_a_replaced_source_is_detected_without_deleting_anything(self):
        self._save()
        state = panoramic_arch_state(self.patient)
        self.assertTrue(arch_describes_source(state, **self._source()))

        replacement = self._file("volume_nifti", "replacement", "c")
        self.assertFalse(
            arch_describes_source(state, **self._source(volume=replacement))
        )
        # The arch itself is untouched: it is the record of what the exported strips
        # were baked from.
        self.assertIsNotNone(panoramic_arch_state(self.patient)["arch"])

    def test_rewritten_volume_bytes_stop_the_arch_describing_them(self):
        """What replaces the delete in ``metadata.update_nifti_metadata``."""
        self._save()
        state = panoramic_arch_state(self.patient)

        self.volume.file_hash = "9" * 64
        self.volume.save(update_fields=["file_hash"])

        self.assertFalse(arch_describes_source(state, **self._source()))

    def test_a_patient_with_no_arch_reports_one_that_matches_nothing(self):
        state = panoramic_arch_state(self.patient)

        self.assertEqual(state["revision"], 0)
        self.assertIsNone(state["arch"])
        self.assertFalse(arch_describes_source(state, **self._source()))

    def test_a_converted_study_edited_live_keeps_one_target_and_one_arch(self):
        """The role has to be the converter's, character for character.

        ``attach_target`` keys on ``(set, resource, role)``, so a live save under a
        different role would create a second target for the same volume -- and the study
        would read back with two arches, one of them the one somebody had replaced.
        """
        from annotations import services
        from annotations.management.commands import annotations_convert_legacy  # noqa: F401

        resource = services.register_logical_volume(
            self.volume, file_key="primary", content_hash=self.volume.file_hash,
            descriptor={"volume_shape": [100, 80, 40]},
        )
        annotation_set = services.get_or_create_set(
            self.patient, PANORAMIC_KIND, check_project=False
        )
        target = services.attach_target(annotation_set, resource, role="volume", primary=True)
        converted = services.record_revision(
            annotation_set, origin=AnnotationOrigin.PREDICTION,
            note="legacy:maxillo.panoramic:1",
        )
        services.apply_descriptors(
            converted, target,
            panoramic_arch(
                [[9, 9], [8, 8], [7, 7], [6, 6]], axial_slice=5,
                volume_shape=[100, 80, 40], geometry_source="auto", default_mode="mip",
                algorithm_version="panorex-js-v2-mip",
            ),
        )

        revision = self._save(expected_revision=1)

        self.assertEqual(
            AnnotationSet.objects.filter(kind=PANORAMIC_KIND, patient=self.patient).count(), 1
        )
        self.assertEqual(
            annotation_set.targets.filter(role="volume", source_resource=resource).count(), 1
        )
        self.assertEqual(Geometry2DItem.objects.filter(revision=revision).count(), 1)
        self.assertEqual(panoramic_arch_state(self.patient)["arch"]["spline"], SPLINE)


class ArchInverseTests(SimpleTestCase):
    def test_a_revision_holding_no_polyline_has_no_arch(self):
        """"No arch" and "an arch with no points" are different answers."""
        self.assertIsNone(arch_from_items([]))
