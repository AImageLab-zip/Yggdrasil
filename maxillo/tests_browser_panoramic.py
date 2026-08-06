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

from common.models import FileRegistry, Job, Modality, Project, ProjectAccess
from maxillo.models import Folder, FolderAccess, PanoramicState, Patient
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
        state = PanoramicState.objects.get(patient=self.patient)
        self.assertEqual(state.revision, 1)
        self.assertEqual(state.source_job, self.job)
        self.assertEqual(state.source_file, self.volume)
        self.assertEqual(state.source_segmentation_file, self.segmentation)
        self.assertEqual(state.generated_by, self.user)
        self.assertEqual(state.default_mode, "mip")
        self.assertEqual(state.algorithm_version, "panorex-js-v2-mip")
        self.assertEqual(state.geometry_source, "auto")
        self.assertEqual(Job.objects.count(), jobs_before)
        rows = FileRegistry.objects.filter(
            metadata__generated_from="browser_cbct_to_panoramic"
        ).order_by("subtype")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(list(rows.values_list("subtype", flat=True)), ["mip", "raysum"])
        self.assertTrue(all(row.processing_job_id is None for row in rows))
        self.assertEqual(rows.get(subtype="mip").metadata["projection"], "maximum")
        self.assertEqual(rows.get(subtype="mip").metadata["interpolation"], "bilinear")
        self.assertEqual(rows.get(subtype="mip").metadata["slab"]["sample_count"], 41)
        self.assertEqual(self.storage.upload_file.call_count, 2)
        self.assertIn(f"patient_{self.patient.patient_id}", state.mip_file.file_path)
        self.assertTrue(state.mip_file.file_path.endswith("/mip.png"))
        self.assertEqual(response.json()["selected_variant"], "mip")
        self.assertEqual(
            {variant["id"] for variant in response.json()["variants"]},
            {"mip", "raysum"},
        )

        descriptor = _panorex_source_data(
            self.patient, _resolved_cbct_viewer_source(self.patient)
        )
        self.assertEqual(descriptor["volumeFileId"], self.volume.id)
        self.assertEqual(descriptor["segmentationFileId"], self.segmentation.id)
        self.assertEqual(descriptor["revision"], 1)
        self.assertEqual(descriptor["state"]["axialSlice"], 20)
        self.assertEqual(descriptor["state"]["geometrySource"], "auto")

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
        self.assertFalse(PanoramicState.objects.exists())
        self.assertFalse(self.storage.upload_file.called)

    def test_source_binding_and_stale_revision_are_conflicts(self):
        wrong_source = self._state()
        wrong_source["source"]["file_hash"] = "c" * 64
        self.assertEqual(self._post(wrong_source).status_code, 409)

        self.assertEqual(self._post().status_code, 200)
        stale = self._state(base_revision=0)
        self.assertEqual(self._post(stale).status_code, 409)
        self.assertEqual(PanoramicState.objects.get().revision, 1)

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
        old_state = PanoramicState.objects.get()
        old_ids = [old_state.mip_file_id, old_state.raysum_file_id]
        old_paths = [old_state.mip_file.file_path, old_state.raysum_file.file_path]
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
        current = PanoramicState.objects.get()
        self.assertEqual(current.revision, 2)
        self.assertEqual(current.default_mode, "raysum")
        self.assertFalse(FileRegistry.objects.filter(id__in=old_ids).exists())
        for path in old_paths:
            self.storage.delete.assert_any_call(path)

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
        old_state = PanoramicState.objects.get(patient=self.patient)
        old_ids = [old_state.mip_file_id, old_state.raysum_file_id]

        replacement_job = Job.objects.create(
            domain="maxillo",
            modality_slug="cbct",
            patient=self.patient,
            status="completed",
            completed_at=old_state.updated_at,
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
        state = PanoramicState.objects.get(patient=self.patient)
        self.assertEqual(state.revision, 1)
        self.assertEqual(state.source_job, replacement_job)
        self.assertEqual(state.source_file, replacement_volume)
        self.assertEqual(state.source_segmentation_file, replacement_segmentation)
        self.assertFalse(FileRegistry.objects.filter(id__in=old_ids).exists())

    def test_model_allows_only_one_active_state_per_patient(self):
        self.assertEqual(self._post().status_code, 200)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PanoramicState.objects.create(
                patient=self.patient,
                source_file=self.volume,
                source_file_key="primary",
                source_file_hash=self.volume.file_hash,
                mip_file=PanoramicState.objects.get().mip_file,
                raysum_file=PanoramicState.objects.get().raysum_file,
                axial_slice=1,
                volume_shape=[100, 80, 40],
                spline=[[1, 1]] * 4,
                default_mode="mip",
                request_hash="d" * 64,
            )

    def test_v1_mean_state_is_stale_preserved_until_v2_replacement(self):
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
        state = PanoramicState.objects.get(patient=self.patient)
        self.assertEqual(state.revision, 1)
        self.assertEqual(state.algorithm_version, "panorex-js-v2-mip")
        self.assertFalse(FileRegistry.objects.filter(id__in=[old_mean.id, old_raysum.id]).exists())
        self.storage.delete.assert_any_call(old_mean.file_path)
        self.storage.delete.assert_any_call(old_raysum.file_path)
