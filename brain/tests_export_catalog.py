"""Brain exports: the folder relation and the project-scoped builder.

Brain patients used to hold folders through a many-to-many; the folder->project
migration collapsed that to a single ``folder`` FK, but the export code kept
querying ``folders__id__in`` — so every brain export raised a FieldError before it
could run. These tests exercise the query paths that were broken.
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from brain.models import Export, Folder, Patient
from common import export_catalog
from common.export_processing import ExportProcessor
from common.models import AnnotationMethod, FileRegistry, Modality, Project, ProjectAccess


class BrainFolderRelationTests(TestCase):
    def setUp(self):
        self.t1, _ = Modality.objects.get_or_create(
            slug="braintumor-mri-t1", defaults={"name": "Brain MRI T1"}
        )
        self.project = Project.objects.create(name="Brain Cat", slug="brain-cat", domain="brain")
        self.project.modalities.set([self.t1])
        self.root = Folder.objects.create(name="Root", project=self.project)
        self.child = Folder.objects.create(name="Child", project=self.project, parent=self.root)
        self.patient = Patient.objects.create(name="Deep", project=self.project, folder=self.child)

    def test_patients_are_found_through_the_single_folder_fk(self):
        export = Export(
            user=None,
            query_params={
                "domain": "brain",
                "folder_ids": [self.root.id],
                "artifacts": ["braintumor-mri-t1.raw"],
            },
        )
        processor = ExportProcessor(export, domain="brain")

        # Used to raise FieldError: Cannot resolve keyword 'folders'.
        self.assertEqual(list(processor.query_patients()), [self.patient])

    def test_collect_files_uses_the_brain_patient_fk(self):
        FileRegistry.objects.create(
            brain_patient=self.patient,
            domain="brain",
            file_type="braintumor_mri_t1_raw",
            file_path="brain/raw/t1/a.nii.gz",
            file_size=11,
            file_hash="a" * 64,
            modality=self.t1,
        )
        export = Export(
            user=None,
            query_params={
                "domain": "brain",
                "folder_ids": [self.child.id],
                "artifacts": ["braintumor-mri-t1.raw"],
            },
        )
        processor = ExportProcessor(export, domain="brain")

        patients = processor.query_patients()
        self.assertEqual(list(patients), [self.patient])
        # The file is not really in storage, so it cannot be collected; what
        # matters is that the query resolves against brain_patient without error.
        entries, _size = processor.collect_files(patients)
        self.assertEqual(entries, [])


class BrainExportBuilderTests(TestCase):
    def setUp(self):
        self.t1, _ = Modality.objects.get_or_create(
            slug="braintumor-mri-t1", defaults={"name": "Brain MRI T1"}
        )
        self.seg, _ = Modality.objects.get_or_create(
            slug="braintumor-mri-seg", defaults={"name": "Brain MRI Segmentation"}
        )
        method, _ = AnnotationMethod.objects.get_or_create(
            slug="voice_caption", defaults={"name": "Voice Captions"}
        )
        self.project = Project.objects.create(name="Brain Build", slug="brain-build", domain="brain")
        self.project.modalities.set([self.t1, self.seg])
        self.project.annotation_methods.set([method])
        self.root = Folder.objects.create(name="Batch", project=self.project)
        self.child = Folder.objects.create(name="Sub", project=self.project, parent=self.root)
        Patient.objects.create(name="P", project=self.project, folder=self.child)

        self.user = User.objects.create_user(username="brain-export", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def test_the_builder_renders_with_the_project_folder_tree(self):
        response = self.client.get(reverse("brain:export_new"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [entry["folder"] for entry in response.context["folders"]], [self.root, self.child]
        )
        self.assertEqual([entry["depth"] for entry in response.context["folders"]], [0, 1])
        # The sub-folder's patient is counted against it.
        self.assertEqual(response.context["folders"][1]["patient_count"], 1)

    def test_only_this_project_mri_channels_are_offered(self):
        response = self.client.get(reverse("brain:export_new"))

        keys = {
            artifact["key"]
            for group in response.context["artifact_groups"]
            for bucket in group["buckets"]
            for artifact in bucket["artifacts"]
        }
        self.assertIn("braintumor-mri-t1.raw", keys)
        self.assertIn("braintumor-mri-seg.processed", keys)
        self.assertNotIn("braintumor-mri-t2.raw", keys)
        # Maxillo artifacts must never appear on a brain project.
        self.assertNotIn("cbct.raw", keys)
        self.assertNotIn("classification.occlusion", keys)

    def test_creating_a_brain_export_records_the_artifacts(self):
        response = self.client.post(
            reverse("brain:export_new"),
            {"folder_ids": [str(self.root.id)], "artifacts": ["braintumor-mri-t1.raw"]},
        )

        self.assertEqual(response.status_code, 302)
        export = Export.objects.get()
        self.assertEqual(export.query_params["artifacts"], ["braintumor-mri-t1.raw"])
        self.assertEqual(export.query_params["project_id"], self.project.id)

    def test_a_maxillo_artifact_is_refused_on_a_brain_project(self):
        response = self.client.post(
            reverse("brain:export_new"),
            {"folder_ids": [str(self.root.id)], "artifacts": ["cbct.raw"]},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Export.objects.exists())

    def test_the_preview_counts_patients_in_sub_folders(self):
        response = self.client.post(
            reverse("brain:export_preview"),
            data={
                "folder_ids": [self.root.id],
                "artifacts": ["braintumor-mri-t1.raw"],
                "filters": {},
            },
            content_type="application/json",
        )

        payload = response.json()
        self.assertTrue(payload["success"], payload)
        self.assertEqual(payload["patient_count"], 1)


class BrainCatalogShapeTests(TestCase):
    def test_every_mri_channel_has_a_raw_and_processed_artifact(self):
        keys = {a.key for a in export_catalog.artifacts_for_domain("brain")}
        for channel in ("t1", "t1c", "t2", "flair", "seg"):
            self.assertIn(f"braintumor-mri-{channel}.raw", keys)
            self.assertIn(f"braintumor-mri-{channel}.processed", keys)

    def test_the_legacy_brain_mapping_still_matches_the_catalog(self):
        from brain.export_config import BRAIN_EXPORT_MODALITY_FILE_TYPES

        self.assertEqual(
            BRAIN_EXPORT_MODALITY_FILE_TYPES["braintumor-mri-t1"],
            {"raw": ["braintumor_mri_t1_raw"], "processed": ["braintumor_mri_t1_processed"]},
        )
