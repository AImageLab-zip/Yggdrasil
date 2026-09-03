import io
import json
import uuid
from unittest import mock

from PIL import Image
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from annotations.constants import (
    AnnotationOrigin,
    CoordinateSystem,
    Geometry2DType,
    PayloadFormat,
    SliceAxis,
)
from annotations.models import AnnotationSet, Geometry2DItem
from common.annotation_lock import annotation_lock_reasons
from common.models import FileRegistry, Job, Modality, Project, ProjectAccess
from maxillo.models import Folder, FolderAccess, PanoramicState, Patient
from maxillo.views.panoramic_state import current_browser_panoramic
from maxillo.views.patient_detail import _panorex_source_data, _resolved_cbct_viewer_source


@override_settings(SECURE_SSL_REDIRECT=False)
class BrowserPanoramicTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(
            slug="maxillo", defaults={"name": "maxillo"}
        )
        if self.project.name != "maxillo":
            self.project.name = "maxillo"
            self.project.save(update_fields=["name"])
        self.user = User.objects.create_user(username="panorex-writer", password="x")
        self.reader = User.objects.create_user(username="panorex-reader", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="annotator")
        ProjectAccess.objects.create(user=self.reader, project=self.project, role="viewer")
        self.folder = Folder.objects.create(name="Panorex cases", project=self.project)
        self.patient = Patient.objects.create(name="Panorex patient", folder=self.folder, project=self.project)
        self.cbct = Modality.objects.create(name="Panorex CBCT", slug="cbct")
        self.panoramic = Modality.objects.create(name="Panorex panoramic", slug="panoramic")
        self.patient.modalities.add(self.cbct)
        self.job = Job.objects.create(
            domain="maxillo",
            modality_slug="cbct",
            patient=self.patient,
            status="completed",
            output_files={"segmentation_nifti": {"label_max": 98}},
        )
        self.volume = FileRegistry.objects.create(
            file_type="cbct_processed",
            subtype="volume_nifti",
            file_path="maxillo/processed/cbct/test-volume.nii.gz",
            file_size=100,
            file_hash="a" * 64,
            metadata={"shape": [100, 80, 40]},
            modality=self.cbct,
            patient=self.patient,
            processing_job=self.job,
        )
        self.segmentation = FileRegistry.objects.create(
            file_type="cbct_processed",
            subtype="segmentation_nifti",
            file_path="maxillo/processed/cbct/test-segmentation.nii.gz",
            file_size=100,
            file_hash="b" * 64,
            modality=self.cbct,
            patient=self.patient,
            processing_job=self.job,
        )
        self.storage = mock.Mock()
        self.client.force_login(self.user)
        self.url = reverse(
            "maxillo:save_browser_panoramic",
            kwargs={"patient_id": self.patient.patient_id},
        )

        for target in (
            "maxillo.views.patient_detail.artifact_exists",
            "maxillo.views.patient_data.artifact_exists",
        ):
            patcher = mock.patch(target, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        storage_patcher = mock.patch(
            "maxillo.views.patient_data.get_object_storage", return_value=self.storage
        )
        storage_patcher.start()
        self.addCleanup(storage_patcher.stop)

    def _png(self, name, size=(120, 40), color=(20, 40, 60, 255)):
        output = io.BytesIO()
        Image.new("RGBA", size, color).save(output, "PNG")
        return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")

    def _state(self, **updates):
        state = {
            "source": {
                "job_id": self.job.id,
                "file_id": self.volume.id,
                "file_key": "primary",
                "file_hash": self.volume.file_hash,
                "segmentation_file_id": self.segmentation.id,
                "segmentation_file_key": "primary",
                "segmentation_file_hash": self.segmentation.file_hash,
            },
            "volume_shape": [100, 80, 40],
            "axial_slice": 20,
            "spline": [[10, 20], [25, 30], [50, 40], [75, 30]],
            "geometry_source": "auto",
            "default_mode": "mip",
            "algorithm_version": "panorex-js-v2-mip",
            "generation_uuid": str(uuid.uuid4()),
            "base_revision": 0,
        }
        state.update(updates)
        return state

    def _arch(self):
        """What the annotation record holds for this patient, as the views read it."""
        return current_browser_panoramic(
            self.patient, _resolved_cbct_viewer_source(self.patient)
        )

    def _annotation_set(self):
        return AnnotationSet.objects.filter(
            patient=self.patient, kind="panoramic_arch"
        ).first()

    def _post(self, state=None, **files):
        return self.client.post(
            self.url,
            data={
                "state": json.dumps(state or self._state()),
                "mip_png": files.get("mip_png", self._png("mip.png")),
                "raysum_png": files.get("raysum_png", self._png("raysum.png")),
            },
        )

    def test_login_csrf_and_write_acl(self):
        anonymous = Client()
        self.assertEqual(anonymous.post(self.url).status_code, 302)

        self.client.force_login(self.reader)
        self.assertEqual(self._post().status_code, 403)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(self.url).status_code, 403)

    def test_success_registers_two_files_state_and_no_job(self):
        jobs_before = Job.objects.count()
        response = self._post()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Job.objects.count(), jobs_before)

        current = self._arch()
        self.assertEqual(current["revision"], 1)
        self.assertTrue(current["matchesSource"])
        self.assertEqual(current["arch"]["axial_slice"], 20)
        self.assertEqual(current["arch"]["geometry_source"], "auto")
        self.assertEqual(current["arch"]["algorithm_version"], "panorex-js-v2-mip")
        self.assertEqual(current["defaultMode"], "mip")

        # The arch is a polyline in one axial slice, and the slice is on the selector --
        # the pairing `validate_item_selector_pairing` refuses to let anyone drop.
        item = Geometry2DItem.objects.get(revision__annotation_set=self._annotation_set())
        self.assertEqual(item.geometry_type, Geometry2DType.POLYLINE)
        self.assertEqual(item.coordinate_system, CoordinateSystem.SLICE_PIXEL)
        self.assertFalse(item.closed)
        self.assertEqual(item.points, [[10, 20], [25, 30], [50, 40], [75, 30]])
        self.assertEqual(item.selector.slice_index, 20)
        self.assertEqual(item.selector.slice_axis, SliceAxis.AXIAL)

        # Both strips hang off the revision as derived renders. Neither is canonical: an
        # image is not the truth about a curve.
        revision = self._annotation_set().revisions.order_by("-revision_number").first()
        payloads = {p.variant: p for p in revision.payloads.all()}
        self.assertEqual(set(payloads), {"mip", "raysum"})
        for payload in payloads.values():
            self.assertEqual(payload.format, PayloadFormat.PNG_RENDER)
            self.assertIsNone(payload.canonical_slot)
            self.assertIsNotNone(payload.file)

        rows = FileRegistry.objects.filter(
            metadata__generated_from="browser_cbct_to_panoramic"
        ).order_by("subtype")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(list(rows.values_list("subtype", flat=True)), ["mip", "raysum"])
        self.assertTrue(all(row.processing_job_id is None for row in rows))
        self.assertEqual(rows.get(subtype="mip").metadata["projection"], "maximum")
        self.assertEqual(rows.get(subtype="mip").metadata["interpolation"], "bilinear")
        self.assertEqual(rows.get(subtype="mip").metadata["slab"]["sample_count"], 41)
        self.assertEqual(rows.get(subtype="mip").metadata["generated_by"], self.user.username)
        self.assertEqual(self.storage.upload_file.call_count, 2)
        mip = current["strips"]["mip"]
        self.assertIn(f"patient_{self.patient.patient_id}", mip.file_path)
        self.assertTrue(mip.file_path.endswith("/mip.png"))
        self.assertEqual(response.json()["selected_variant"], "mip")
        self.assertEqual(
            {variant["id"] for variant in response.json()["variants"]},
            {"mip", "raysum"},
        )

        # Nothing is written to the frozen legacy table any more.
        self.assertFalse(PanoramicState.objects.exists())

        descriptor = _panorex_source_data(
            self.patient, _resolved_cbct_viewer_source(self.patient)
        )
        self.assertEqual(descriptor["volumeFileId"], self.volume.id)
        self.assertEqual(descriptor["segmentationFileId"], self.segmentation.id)
        self.assertEqual(descriptor["revision"], 1)
        self.assertEqual(descriptor["state"]["axialSlice"], 20)
        self.assertEqual(descriptor["state"]["geometrySource"], "auto")
        self.assertEqual(descriptor["state"]["spline"], [[10, 20], [25, 30], [50, 40], [75, 30]])

    def test_an_auto_arch_is_machine_output_and_never_locks_the_raw_data(self):
        """The single most consequential line in the service.

        ``panoramic_warmup`` drives every patient in a folder through this endpoint with
        an ``auto`` arch. Recorded as human work, one warm-up run would freeze the raw
        data of every case it touched -- and decision #18 made the lock monotonic, so
        nothing would thaw it again.
        """
        self.assertEqual(self._post().status_code, 200)

        annotation_set = self._annotation_set()
        revision = annotation_set.revisions.get()
        self.assertEqual(revision.origin, AnnotationOrigin.PREDICTION)
        self.assertFalse(annotation_set.ever_annotated)
        self.assertEqual(list(annotation_lock_reasons(self.patient)), [])

    def test_an_edited_arch_is_human_work_and_locks_the_raw_data(self):
        edited = self._state(geometry_source="custom_cp")
        self.assertEqual(self._post(edited).status_code, 200)

        annotation_set = self._annotation_set()
        self.assertEqual(annotation_set.revisions.get().origin, AnnotationOrigin.MANUAL)
        self.assertTrue(annotation_set.ever_annotated)
        self.assertIn("an edited panoramic arch", list(annotation_lock_reasons(self.patient)))

    def test_the_arch_names_both_the_volume_and_the_segmentation_it_was_fitted_to(self):
        self.assertEqual(self._post().status_code, 200)

        roles = {
            target.role: target.source_resource
            for target in self._annotation_set().targets.select_related("source_resource")
        }
        self.assertEqual(set(roles), {"volume", "segmentation"})
        self.assertEqual(roles["volume"].file_id, self.volume.id)
        self.assertEqual(roles["segmentation"].file_id, self.segmentation.id)
        # The fingerprint is what makes a replaced source detectable without a delete.
        revision = self._annotation_set().revisions.get()
        self.assertEqual(
            set(revision.source_fingerprint.values()),
            {self.volume.file_hash, self.segmentation.file_hash},
        )

    def test_source_shape_spline_png_and_mode_validation(self):
        cases = [
            (self._state(volume_shape=[99, 80, 40]), {}, 409),
            (self._state(axial_slice=40), {}, 400),
            (self._state(spline=[[1, 1], [2, 2], [3, 3]]), {}, 400),
            (self._state(spline=[[1, 1], [2, 2], [3, 3], [100, 4]]), {}, 400),
            (self._state(default_mode="mean"), {}, 400),
            (self._state(algorithm_version="other"), {}, 400),
            (self._state(), {"mip_png": SimpleUploadedFile("bad.png", b"not png")}, 400),
            (self._state(geometry_source="other"), {}, 400),
            (self._state(), {"raysum_png": self._png("other.png", size=(121, 40))}, 400),
            (self._state(), {"mip_png": self._png("short.png", size=(120, 39))}, 400),
        ]
        for state, files, expected in cases:
            with self.subTest(expected=expected, files=files, state=state):
                self.assertEqual(self._post(state, **files).status_code, expected)
        self.assertIsNone(self._arch()["arch"])
        self.assertFalse(self.storage.upload_file.called)

    def test_source_binding_and_stale_revision_are_conflicts(self):
        wrong_source = self._state()
        wrong_source["source"]["file_hash"] = "c" * 64
        self.assertEqual(self._post(wrong_source).status_code, 409)

        self.assertEqual(self._post().status_code, 200)
        stale = self._state(base_revision=0)
        self.assertEqual(self._post(stale).status_code, 409)
        self.assertEqual(self._arch()["revision"], 1)

    def test_generation_uuid_is_idempotent_only_for_identical_request(self):
        state = self._state()
        first = self._post(state)
        second = self._post(state)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["idempotent"])
        self.assertEqual(self.storage.upload_file.call_count, 2)
        changed = dict(state, axial_slice=21)
        self.assertEqual(self._post(changed).status_code, 409)

    def test_viewer_source_descriptor_can_be_posted_back_and_old_outputs_are_cleaned(self):
        first = self._state()
        self.assertEqual(self._post(first).status_code, 200)
        old_strips = self._arch()["strips"]
        old_ids = [row.id for row in old_strips.values()]
        old_paths = [row.file_path for row in old_strips.values()]
        descriptor = _panorex_source_data(
            self.patient, _resolved_cbct_viewer_source(self.patient)
        )
        second = self._state(
            source=descriptor,
            base_revision=descriptor["revision"],
            default_mode="raysum",
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self._post(second)

        self.assertEqual(response.status_code, 200, response.content)
        current = self._arch()
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["defaultMode"], "raysum")
        # A superseded strip is deleted, bytes and payload row together: it is derived,
        # regenerable from the arch, and `AnnotationPayload.file` is PROTECT, so the
        # payload has to go first or the FileRegistry delete raises.
        self.assertFalse(FileRegistry.objects.filter(id__in=old_ids).exists())
        for path in old_paths:
            self.storage.delete.assert_any_call(path)
        # The arch itself is history and stays: two revisions, one set.
        self.assertEqual(self._annotation_set().revisions.count(), 2)

    def test_active_state_has_serving_precedence_for_default_and_alternate(self):
        state = self._state(default_mode="raysum")
        self.assertEqual(self._post(state).status_code, 200)
        url = reverse(
            "maxillo:patient_panoramic_data",
            kwargs={"patient_id": self.patient.patient_id},
        )

        default = self.client.get(url, {"meta": "1"})
        alternate = self.client.get(url, {"meta": "1", "variant": "mip"})

        self.assertEqual(default.status_code, 200)
        self.assertEqual(default.json()["selected_variant"], "raysum")
        self.assertEqual(alternate.json()["selected_variant"], "mip")
        self.assertFalse(default.json()["editable"])
        self.assertIsNone(default.json()["raw_url"])
        self.assertEqual({item["id"] for item in default.json()["variants"]}, {"mip", "raysum"})
        serialized = default.content.decode("utf-8")
        self.assertNotIn("maxillo/processed/panoramic", serialized)

    def test_replaced_source_prevents_stale_state_serving(self):
        self.assertEqual(self._post().status_code, 200)
        self.job.status = "failed"
        self.job.save(update_fields=["status"])

        response = self.client.get(
            reverse(
                "maxillo:patient_panoramic_data",
                kwargs={"patient_id": self.patient.patient_id},
            ),
            {"meta": "1"},
        )
        self.assertEqual(response.status_code, 404)

    def test_legacy_generated_panoramic_from_old_source_is_not_served(self):
        FileRegistry.objects.create(
            file_type="panoramic_processed",
            subtype="panoramic_png",
            file_path="maxillo/processed/panoramic/stale.png",
            file_size=12,
            file_hash="9" * 64,
            metadata={
                "generated_from": "cbct_to_panoramic",
                "is_default": True,
                "input_files": {
                    "volume_nifti": "maxillo/processed/cbct/old-volume.nii.gz",
                    "segmentation_nifti": "maxillo/processed/cbct/old-seg.nii.gz",
                },
                "files": {
                    "panoramic_png": {
                        "path": "maxillo/processed/panoramic/stale.png"
                    }
                },
            },
            modality=self.panoramic,
            patient=self.patient,
        )

        response = self.client.get(
            reverse(
                "maxillo:patient_panoramic_data",
                kwargs={"patient_id": self.patient.patient_id},
            ),
            {"meta": "1"},
        )

        self.assertEqual(response.status_code, 404)

    def test_replaced_source_can_save_a_new_revision_zero_state(self):
        self.assertEqual(self._post().status_code, 200)
        old_ids = [row.id for row in self._arch()["strips"].values()]
        first_revision_at = self._annotation_set().updated_at

        replacement_job = Job.objects.create(
            domain="maxillo",
            modality_slug="cbct",
            patient=self.patient,
            status="completed",
            completed_at=first_revision_at,
            output_files={"segmentation_nifti": {"label_max": 98}},
        )
        replacement_volume = FileRegistry.objects.create(
            file_type="cbct_processed",
            subtype="volume_nifti",
            file_path="maxillo/processed/cbct/replacement-volume.nii.gz",
            file_size=100,
            file_hash="c" * 64,
            metadata={"shape": [100, 80, 40]},
            modality=self.cbct,
            patient=self.patient,
            processing_job=replacement_job,
        )
        replacement_segmentation = FileRegistry.objects.create(
            file_type="cbct_processed",
            subtype="segmentation_nifti",
            file_path="maxillo/processed/cbct/replacement-segmentation.nii.gz",
            file_size=100,
            file_hash="d" * 64,
            modality=self.cbct,
            patient=self.patient,
            processing_job=replacement_job,
        )
        source = _panorex_source_data(
            self.patient, _resolved_cbct_viewer_source(self.patient)
        )
        self.assertEqual(source["revision"], 0)
        replacement = self._state(
            source=source,
            base_revision=0,
            generation_uuid=str(uuid.uuid4()),
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self._post(replacement)

        self.assertEqual(response.status_code, 200, response.content)
        current = self._arch()
        self.assertTrue(current["matchesSource"])
        # The client quoted 0 and starts again; the server's own count keeps going up,
        # because the unique constraint on (set, revision_number) is what makes a stale
        # writer lose, and rewinding it would hand two editors the same number.
        self.assertEqual(current["revision"], 2)
        self.assertEqual(_panorex_source_data(
            self.patient, _resolved_cbct_viewer_source(self.patient)
        )["revision"], 2)
        files = {
            target.role: target.source_resource.file_id
            for target in self._annotation_set().targets.select_related("source_resource")
            if target.source_resource.file_id
            in {replacement_volume.id, replacement_segmentation.id}
        }
        self.assertEqual(
            files, {"volume": replacement_volume.id, "segmentation": replacement_segmentation.id}
        )
        self.assertFalse(FileRegistry.objects.filter(id__in=old_ids).exists())

    def test_one_set_per_patient_and_revision_numbers_never_repeat(self):
        """What replaced ``PanoramicState``'s one-row-per-patient constraint.

        The uniqueness that matters is no longer "one row": it is one *set* per
        ``(domain, patient, kind)`` with monotonically numbered revisions, and the unique
        constraint on ``(annotation_set, revision_number)`` is the optimistic-concurrency
        primitive -- there is no read-then-write window, because the check is the write.
        """
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(self._post(self._state(base_revision=1)).status_code, 200)

        self.assertEqual(
            AnnotationSet.objects.filter(
                patient=self.patient, kind="panoramic_arch"
            ).count(),
            1,
        )
        annotation_set = self._annotation_set()
        self.assertEqual(
            list(annotation_set.revisions.order_by("revision_number").values_list(
                "revision_number", flat=True
            )),
            [1, 2],
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            annotation_set.revisions.create(revision_number=2)

    def test_a_stale_v1_arch_is_not_served_and_its_strips_go_with_the_v2_save(self):
        """A v1 arch is history, and its strips must not survive into an export.

        The legacy row is frozen now, so the strips it points at are reachable from
        nowhere else -- and ``common/export_catalog.py`` collects ``panoramic_processed``
        rows by subtype, so a surviving v1 ``raysum`` would be exported *beside* the
        current one.
        """
        old_mean = FileRegistry.objects.create(
            file_type="panoramic_processed",
            subtype="mean",
            file_path="maxillo/processed/panoramic/old-v1/mean.png",
            file_size=12,
            file_hash="e" * 64,
            metadata={"generated_from": "browser_cbct_to_panoramic", "variant": "mean"},
            modality=self.panoramic,
            patient=self.patient,
        )
        old_raysum = FileRegistry.objects.create(
            file_type="panoramic_processed",
            subtype="raysum",
            file_path="maxillo/processed/panoramic/old-v1/raysum.png",
            file_size=12,
            file_hash="f" * 64,
            metadata={"generated_from": "browser_cbct_to_panoramic", "variant": "raysum"},
            modality=self.panoramic,
            patient=self.patient,
        )
        PanoramicState.objects.create(
            patient=self.patient,
            source_job=self.job,
            source_file=self.volume,
            source_file_key="primary",
            source_file_hash=self.volume.file_hash,
            source_segmentation_file=self.segmentation,
            source_segmentation_key="primary",
            source_segmentation_hash=self.segmentation.file_hash,
            mip_file=old_mean,
            raysum_file=old_raysum,
            axial_slice=20,
            volume_shape=[100, 80, 40],
            spline=[[10, 20], [25, 30], [50, 40], [75, 30]],
            geometry_source="auto",
            default_mode="mip",
            algorithm_version="panorex-js-v1",
            revision=4,
            request_hash="1" * 64,
        )
        serve_url = reverse(
            "maxillo:patient_panoramic_data",
            kwargs={"patient_id": self.patient.patient_id},
        )

        self.assertEqual(self.client.get(serve_url, {"meta": "1"}).status_code, 404)
        self.assertEqual(self._post(self._state(default_mode="mean")).status_code, 400)
        self.assertEqual(FileRegistry.objects.filter(id__in=[old_mean.id, old_raysum.id]).count(), 2)

        with self.captureOnCommitCallbacks(execute=True):
            response = self._post(self._state(base_revision=0))

        self.assertEqual(response.status_code, 200, response.content)
        current = self._arch()
        self.assertEqual(current["revision"], 1)
        self.assertEqual(current["arch"]["algorithm_version"], "panorex-js-v2-mip")
        self.assertFalse(FileRegistry.objects.filter(id__in=[old_mean.id, old_raysum.id]).exists())
        self.storage.delete.assert_any_call(old_mean.file_path)
        self.storage.delete.assert_any_call(old_raysum.file_path)
