from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from common.models import FileRegistry, Job, Modality, Project, ProjectAccess
from maxillo import file_utils
from maxillo.file_utils import mark_job_completed
from maxillo.models import Patient


class PanoramicProcessingTests(TestCase):
    def setUp(self):
        send_task = mock.patch("common.signals.celery_app.send_task")
        send_task.start()
        self.addCleanup(send_task.stop)

        exists = mock.patch("maxillo.file_utils.artifact_exists", return_value=True)
        exists.start()
        self.addCleanup(exists.stop)

        storage = mock.patch(
            "maxillo.file_utils.get_object_storage",
            return_value=mock.Mock(
                head=mock.Mock(side_effect=Exception("no storage in test"))
            ),
        )
        storage.start()
        self.addCleanup(storage.stop)

        self.patient = Patient.objects.create(name="Panoramic Patient")
        self.modality = Modality.objects.create(name="Panoramic", slug="panoramic")

    def test_completion_registers_only_known_variants_with_shared_metadata(self):
        known_keys = tuple(file_utils.PANORAMIC_OUTPUT_KEYS)
        self.assertIn(file_utils.DEFAULT_PANORAMIC_OUTPUT, known_keys)
        additional_key = next(
            key for key in known_keys if key != file_utils.DEFAULT_PANORAMIC_OUTPUT
        )
        selected_keys = (file_utils.DEFAULT_PANORAMIC_OUTPUT, additional_key)
        outputs = {
            key: f"processed/panoramic/{key}.png" for key in selected_keys
        }
        outputs["debug_artifact"] = "processed/panoramic/debug.json"
        job = Job.objects.create(
            domain="maxillo",
            modality_slug="cbct_to_panoramic",
            patient=self.patient,
            status="processing",
            input_files={"volume_nifti": "processed/cbct/volume.nii.gz"},
        )

        mark_job_completed(job.id, outputs, logs="panoramic complete")

        job.refresh_from_db()
        self.assertEqual(set(job.output_files), set(selected_keys))
        rows = FileRegistry.objects.filter(processing_job=job).order_by("subtype")
        self.assertEqual(rows.count(), len(selected_keys))
        self.assertEqual(set(rows.values_list("subtype", flat=True)), set(selected_keys))
        for row in rows:
            self.assertEqual(row.file_type, "panoramic_processed")
            self.assertEqual(row.modality, self.modality)
            self.assertEqual(row.metadata["generated_from"], "cbct_to_panoramic")
            self.assertEqual(row.metadata["panoramic_output"], row.subtype)
            self.assertEqual(row.metadata["files"].keys(), outputs.keys() - {"debug_artifact"})
            self.assertEqual(
                row.metadata["is_default"],
                row.subtype == file_utils.DEFAULT_PANORAMIC_OUTPUT,
            )


@override_settings(SECURE_SSL_REDIRECT=False)
class PanoramicVariantEndpointTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(
            slug="maxillo", defaults={"name": "maxillo"}
        )
        if self.project.name != "maxillo":
            self.project.name = "maxillo"
            self.project.save(update_fields=["name"])
        self.user = User.objects.create_user(username="panoramic-admin", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(name="Panoramic Endpoint Patient")
        self.modality = Modality.objects.create(name="Panoramic Endpoint", slug="panoramic")

    @mock.patch("maxillo.views.patient_data.artifact_exists", return_value=True)
    def test_metadata_lists_registered_variants_and_selects_default(self, _exists):
        files = {
            "panoramic_png": {
                "path": "processed/panoramic/default.png",
                "type": "panoramic_png",
            },
            "panoramic_zplus20_raysum_png": {
                "path": "processed/panoramic/zplus20-raysum.png",
                "type": "panoramic_zplus20_raysum_png",
            },
        }
        FileRegistry.objects.create(
            file_type="panoramic_processed",
            subtype="panoramic_png",
            file_path=files["panoramic_png"]["path"],
            file_size=1,
            file_hash="panoramic",
            patient=self.patient,
            modality=self.modality,
            metadata={
                "generated_from": "cbct_to_panoramic",
                "is_default": True,
                "files": files,
            },
        )

        response = self.client.get(
            reverse(
                "maxillo:patient_panoramic_data",
                kwargs={"patient_id": self.patient.patient_id},
            ),
            {"meta": "1"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["selected_variant"], "z0_mean")
        self.assertEqual(
            {variant["id"] for variant in body["variants"]},
            {"z0_mean", "zplus20_raysum"},
        )
        self.assertFalse(body["editable"])
        self.assertIsNone(body["raw_url"])
