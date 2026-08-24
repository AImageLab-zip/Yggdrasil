"""Project-driven export: artifact catalog, filters, and the export builder."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from common import export_catalog, export_ui
from common.export_processing import ExportProcessor
from common.models import (
    AnnotationMethod,
    FileRegistry,
    Job,
    Modality,
    Project,
    ProjectAccess,
)
from maxillo.models import Classification, Export, Folder, Patient


def _method(slug, name):
    method, _ = AnnotationMethod.objects.get_or_create(slug=slug, defaults={"name": name})
    return method


def _modality(slug, name):
    modality, _ = Modality.objects.get_or_create(slug=slug, defaults={"name": name})
    return modality


class ArtifactCatalogTests(TestCase):
    def test_a_cbct_project_sees_only_cbct_and_panoramic_artifacts(self):
        artifacts = export_catalog.artifacts_for_project("maxillo", ["cbct", "panoramic"])
        modalities = {a.modality for a in artifacts}

        self.assertEqual(modalities, {"cbct", "panoramic", None})
        keys = {a.key for a in artifacts}
        self.assertIn("cbct.segmentation", keys)
        self.assertIn("panoramic.mip", keys)
        self.assertNotIn("ios.raw", keys)
        self.assertNotIn("intraoral-photo.raw", keys)

    def test_brain_artifacts_never_leak_into_maxillo(self):
        maxillo_keys = {a.key for a in export_catalog.artifacts_for_domain("maxillo")}
        brain_keys = {a.key for a in export_catalog.artifacts_for_domain("brain")}

        self.assertTrue(any(key.startswith("braintumor-mri-") for key in brain_keys))
        self.assertFalse(any(key.startswith("braintumor-mri-") for key in maxillo_keys))

    def test_unknown_artifact_keys_are_dropped_not_fatal(self):
        artifacts = export_catalog.resolve_artifacts("maxillo", ["cbct.raw", "retired.thing"])
        self.assertEqual([a.key for a in artifacts], ["cbct.raw"])

    def test_panoramic_variants_are_separable(self):
        mip = export_catalog.artifact_by_key("maxillo", "panoramic.mip")
        raysum = export_catalog.artifact_by_key("maxillo", "panoramic.raysum")
        legacy = export_catalog.artifact_by_key("maxillo", "panoramic.legacy")

        class Row:
            metadata = {}

            def __init__(self, file_type, subtype):
                self.file_type = file_type
                self.subtype = subtype

        self.assertTrue(mip.matches(Row("panoramic_processed", "mip")))
        self.assertFalse(mip.matches(Row("panoramic_processed", "raysum")))
        self.assertTrue(raysum.matches(Row("panoramic_processed", "raysum")))
        # An older sweep variant belongs to neither of the two current ones.
        self.assertFalse(mip.matches(Row("panoramic_processed", "panoramic_zplus20_mean_png")))
        self.assertTrue(legacy.matches(Row("panoramic_processed", "panoramic_zplus20_mean_png")))

    def test_legacy_query_params_resolve_to_the_same_content(self):
        artifacts = export_catalog.artifacts_from_legacy_selection(
            "maxillo",
            ["cbct"],
            include_raw=True,
            include_processed=False,
            include_reports=False,
            include_bite_classification=False,
        )
        self.assertEqual([a.key for a in artifacts], ["cbct.raw"])

        with_processed = export_catalog.artifacts_from_legacy_selection(
            "maxillo",
            ["cbct"],
            include_raw=False,
            include_processed=True,
            include_reports=False,
            include_bite_classification=False,
        )
        keys = {a.key for a in with_processed}
        self.assertIn("cbct.volume", keys)
        self.assertIn("cbct.segmentation", keys)
        self.assertNotIn("cbct.raw", keys)

    def test_legacy_bite_classification_flag_maps_to_the_occlusion_document(self):
        artifacts = export_catalog.artifacts_from_legacy_selection(
            "maxillo", [],
            include_raw=False, include_processed=False,
            include_reports=False, include_bite_classification=True,
        )
        self.assertEqual([a.key for a in artifacts], ["classification.occlusion"])


class FilterCatalogTests(TestCase):
    def setUp(self):
        self.cbct = _modality("cbct", "CBCT")
        self.ios = _modality("ios", "IOS")
        self.project = Project.objects.create(name="Filters", slug="cat-filters", domain="maxillo")
        self.project.modalities.set([self.cbct])
        self.project.annotation_methods.set([_method("voice_caption", "Voice Captions")])

    def test_filters_only_cover_what_the_project_collects(self):
        specs = export_catalog.build_filters("maxillo", self.project, ["cbct"])
        ids = [spec["id"] for spec in specs]

        self.assertIn("modality_cbct", ids)
        self.assertIn("annotation_captions", ids)
        self.assertNotIn("modality_ios", ids)
        self.assertNotIn("annotation_landmarks", ids)
        self.assertNotIn("annotation_occlusion", ids)

    def test_panoramic_state_filter_needs_cbct(self):
        with_cbct = [s["id"] for s in export_catalog.build_filters("maxillo", self.project, ["cbct"])]
        without = [s["id"] for s in export_catalog.build_filters("maxillo", self.project, ["panoramic"])]

        self.assertIn("panoramic_state", with_cbct)
        self.assertNotIn("panoramic_state", without)

    def test_legacy_filter_keys_are_translated(self):
        normalized = export_catalog.normalize_filters({
            "has_cbct": True,
            "has_bite_classification": True,
            "has_reports_cbct": True,
        })
        self.assertEqual(
            normalized,
            {
                "modality_cbct": True,
                "annotation_occlusion": True,
                "annotation_captions": True,
            },
        )

    def test_form_reading_keeps_only_prefixed_fields(self):
        # A submitted form also carries folder_ids / artifacts / the CSRF token;
        # none of those may end up stored as a filter.
        normalized = export_catalog.filters_from_form({
            "filter_modality_cbct": "1",
            "filter_uploaded_by": "",
            "filter_status_cbct": "failed",
            "folder_ids": "7",
            "artifacts": "cbct.raw",
            "csrfmiddlewaretoken": "abc",
        })
        self.assertEqual(
            normalized, {"modality_cbct": "1", "status_cbct": "failed"}
        )

    def test_keyed_payloads_still_accept_a_stray_prefix(self):
        self.assertEqual(
            export_catalog.normalize_filters({"filter_modality_cbct": "1"}),
            {"modality_cbct": "1"},
        )


class FilterApplicationTests(TestCase):
    def setUp(self):
        self.cbct = _modality("cbct", "CBCT")
        self.project = Project.objects.create(name="Apply", slug="apply-filters", domain="maxillo")
        self.project.modalities.set([self.cbct])
        self.folder = Folder.objects.create(name="Batch", project=self.project)

        self.with_cbct = Patient.objects.create(name="HasCBCT", project=self.project, folder=self.folder)
        self.without = Patient.objects.create(name="Empty", project=self.project, folder=self.folder)
        FileRegistry.objects.create(
            patient=self.with_cbct, domain="maxillo", file_type="cbct_raw",
            file_path="maxillo/raw/cbct/a.nii.gz", file_size=10, file_hash="a" * 64,
            modality=self.cbct,
        )

    def _apply(self, filters, artifacts=()):
        return export_catalog.apply_filters(
            Patient.objects.filter(project=self.project), "maxillo", filters,
            artifacts=artifacts,
        )

    def test_modality_presence(self):
        result = self._apply({"modality_cbct": True})
        self.assertEqual(list(result), [self.with_cbct])

    def test_processing_status_none_and_failed(self):
        Job.objects.create(
            domain="maxillo", patient=self.with_cbct, modality_slug="cbct", status="failed"
        )
        self.assertEqual(list(self._apply({"status_cbct": "failed"})), [self.with_cbct])
        self.assertEqual(list(self._apply({"status_cbct": "none"})), [self.without])

    def test_annotation_presence(self):
        Classification.objects.create(
            patient=self.with_cbct, classifier="manual",
            sagittal_left="I", sagittal_right="I", vertical="normal",
            transverse="normal", midline="centered",
        )
        self.assertEqual(
            list(self._apply({"annotation_occlusion": True})), [self.with_cbct]
        )

    def test_uploaded_by_and_tags(self):
        user = User.objects.create_user(username="uploader", password="x")
        self.with_cbct.uploaded_by = user
        self.with_cbct.save(update_fields=["uploaded_by"])
        self.assertEqual(
            list(self._apply({"uploaded_by": "uploader"})), [self.with_cbct]
        )
        self.assertEqual(list(self._apply({"uploaded_by": "nobody"})), [])


class ExportBuilderViewTests(TestCase):
    def setUp(self):
        self.cbct = _modality("cbct", "CBCT")
        self.panoramic = _modality("panoramic", "Panoramic")
        self.ios = _modality("ios", "IOS")
        self.project = Project.objects.create(name="TF4 Export", slug="tf4-export", domain="maxillo")
        self.project.modalities.set([self.cbct, self.panoramic])
        self.project.annotation_methods.set([_method("voice_caption", "Voice Captions")])
        self.other_project = Project.objects.create(name="Other Export", slug="other-export", domain="maxillo")

        self.root = Folder.objects.create(name="Batch", project=self.project)
        self.child = Folder.objects.create(name="Sub", project=self.project, parent=self.root)
        self.foreign = Folder.objects.create(name="Foreign", project=self.other_project)

        self.admin = User.objects.create_user(username="export-admin", password="x")
        ProjectAccess.objects.create(user=self.admin, project=self.project, role="admin")
        self.client.force_login(self.admin)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def test_the_builder_lists_only_this_project_folders_including_subfolders(self):
        response = self.client.get(reverse("maxillo:export_new"))

        self.assertEqual(response.status_code, 200)
        listed = [entry["folder"] for entry in response.context["folders"]]
        self.assertEqual(listed, [self.root, self.child])
        self.assertNotIn(self.foreign, listed)
        # Sub-folders are indented rather than hidden.
        self.assertEqual(
            [entry["depth"] for entry in response.context["folders"]], [0, 1]
        )

    def test_the_builder_offers_only_this_project_artifacts(self):
        response = self.client.get(reverse("maxillo:export_new"))

        keys = {
            artifact["key"]
            for group in response.context["artifact_groups"]
            for bucket in group["buckets"]
            for artifact in bucket["artifacts"]
        }
        self.assertIn("cbct.raw", keys)
        self.assertIn("panoramic.mip", keys)
        self.assertNotIn("ios.raw", keys)
        self.assertNotContains(response, "ios.landmarks")

    def test_artifacts_with_nothing_stored_are_offered_but_unavailable(self):
        patient = Patient.objects.create(name="P", project=self.project, folder=self.root)
        FileRegistry.objects.create(
            patient=patient, domain="maxillo", file_type="cbct_raw",
            file_path="maxillo/raw/cbct/p.nii.gz", file_size=10, file_hash="b" * 64,
            modality=self.cbct,
        )

        response = self.client.get(reverse("maxillo:export_new"))
        artifacts = {
            artifact["key"]: artifact
            for group in response.context["artifact_groups"]
            for bucket in group["buckets"]
            for artifact in bucket["artifacts"]
        }
        self.assertEqual(artifacts["cbct.raw"]["count"], 1)
        self.assertTrue(artifacts["cbct.raw"]["available"])
        self.assertEqual(artifacts["panoramic.mip"]["count"], 0)
        self.assertFalse(artifacts["panoramic.mip"]["available"])

    def test_creating_an_export_records_the_artifact_selection(self):
        response = self.client.post(
            reverse("maxillo:export_new"),
            {
                "folder_ids": [str(self.root.id)],
                "artifacts": ["cbct.raw", "panoramic.mip"],
                "filter_modality_cbct": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        export = Export.objects.get()
        self.assertEqual(export.query_params["artifacts"], ["cbct.raw", "panoramic.mip"])
        self.assertEqual(export.query_params["project_id"], self.project.id)
        self.assertEqual(export.query_params["filters"], {"modality_cbct": "1"})
        self.assertIn("Uploaded volume", export.query_summary)

    def test_an_artifact_the_project_does_not_enable_is_refused(self):
        response = self.client.post(
            reverse("maxillo:export_new"),
            {"folder_ids": [str(self.root.id)], "artifacts": ["ios.raw"]},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Export.objects.exists())

    def test_a_folder_from_another_project_is_refused(self):
        response = self.client.post(
            reverse("maxillo:export_new"),
            {"folder_ids": [str(self.foreign.id)], "artifacts": ["cbct.raw"]},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Export.objects.exists())

    def test_the_preview_counts_the_same_artifacts_the_export_would_write(self):
        patient = Patient.objects.create(name="P", project=self.project, folder=self.child)
        FileRegistry.objects.create(
            patient=patient, domain="maxillo", file_type="cbct_raw",
            file_path="maxillo/raw/cbct/preview.nii.gz", file_size=1234, file_hash="c" * 64,
            modality=self.cbct,
        )

        # The parent folder is selected; the patient lives in its sub-folder.
        response = self.client.post(
            reverse("maxillo:export_preview"),
            data={"folder_ids": [self.root.id], "artifacts": ["cbct.raw"], "filters": {}},
            content_type="application/json",
        )

        payload = response.json()
        self.assertTrue(payload["success"], payload)
        self.assertEqual(payload["patient_count"], 1, "sub-folder patients must be in scope")
        self.assertEqual(payload["file_count"], 1)
        self.assertEqual(payload["estimated_size_bytes"], 1234)


class ExportProcessorSelectionTests(TestCase):
    def setUp(self):
        self.cbct = _modality("cbct", "CBCT")
        self.panoramic = _modality("panoramic", "Panoramic")
        self.project = Project.objects.create(name="Proc", slug="proc-export", domain="maxillo")
        self.project.modalities.set([self.cbct, self.panoramic])
        self.root = Folder.objects.create(name="Root", project=self.project)
        self.child = Folder.objects.create(name="Child", project=self.project, parent=self.root)
        self.patient = Patient.objects.create(name="Deep", project=self.project, folder=self.child)

    def _processor(self, **query_params):
        export = Export(user=None, query_params={"domain": "maxillo", **query_params})
        return ExportProcessor(export, domain="maxillo")

    def test_selecting_a_parent_folder_includes_its_children(self):
        processor = self._processor(folder_ids=[self.root.id], artifacts=["cbct.raw"])
        self.assertEqual(list(processor.query_patients()), [self.patient])

    def test_artifact_keys_drive_the_modality_list(self):
        processor = self._processor(
            folder_ids=[self.root.id], artifacts=["cbct.segmentation", "panoramic.mip"]
        )
        self.assertEqual(processor.modality_slugs, ["cbct", "panoramic"])

    def test_legacy_rows_without_artifacts_still_resolve(self):
        processor = self._processor(
            folder_ids=[self.root.id],
            modality_slugs=["cbct"],
            include_raw=True,
            include_processed=False,
        )
        self.assertEqual([a.key for a in processor.artifacts], ["cbct.raw"])

    def test_only_the_selected_panoramic_variant_is_collected(self):
        for subtype, name in (("mip", "mip.png"), ("raysum", "raysum.png")):
            FileRegistry.objects.create(
                patient=self.patient, domain="maxillo", file_type="panoramic_processed",
                subtype=subtype, file_path=f"maxillo/processed/panoramic/{name}",
                file_size=5, file_hash=(subtype * 12)[:64].ljust(64, "0"), modality=self.panoramic,
                metadata={"generated_from": "browser_cbct_to_panoramic"},
            )

        processor = self._processor(folder_ids=[self.root.id], artifacts=["panoramic.mip"])
        entries, _size = processor.collect_files(processor.query_patients())

        # The files are not really in storage, so nothing is collectable; what
        # matters is that only the MIP row was ever considered.
        self.assertTrue(all(e["artifact"].key == "panoramic.mip" for e in entries))
        selected = [a.key for a in processor.artifacts]
        self.assertEqual(selected, ["panoramic.mip"])

    def test_an_empty_selection_fails_the_export_with_a_clear_message(self):
        processor = self._processor(folder_ids=[self.root.id], artifacts=[])
        self.assertEqual(processor.artifacts, [])


class ExportUiHelperTests(TestCase):
    def test_folder_tree_treats_an_unreachable_parent_as_a_root(self):
        project = Project.objects.create(name="Tree", slug="tree-export", domain="maxillo")
        hidden_parent = Folder.objects.create(name="Hidden", project=project)
        visible_child = Folder.objects.create(name="Visible", project=project, parent=hidden_parent)

        tree = export_ui.folder_tree([visible_child], Patient, "maxillo")

        self.assertEqual([entry["folder"] for entry in tree], [visible_child])
        self.assertEqual(tree[0]["depth"], 0)


class CbctBundleShapeTests(TestCase):
    """`cbct_processed` rows exist in three historical shapes, all still live."""

    def setUp(self):
        self.segmentation = export_catalog.artifact_by_key("maxillo", "cbct.segmentation")
        self.volume = export_catalog.artifact_by_key("maxillo", "cbct.volume")
        self.pipeline_panoramic = export_catalog.artifact_by_key("maxillo", "cbct.panoramic_view")

    class Row:
        def __init__(self, subtype="", file_path="", file_size=0, metadata=None):
            self.file_type = "cbct_processed"
            self.subtype = subtype
            self.file_path = file_path
            self.file_size = file_size
            self.metadata = metadata or {}

    def test_current_shape_one_row_per_output_named_by_subtype(self):
        row = self.Row(
            subtype="segmentation_nifti",
            file_path="maxillo/processed/cbct/job_1/predictions/p.nii.gz",
            file_size=42,
        )

        self.assertTrue(self.segmentation.matches(row))
        self.assertEqual(
            self.segmentation.resolve_output(row),
            {"path": "maxillo/processed/cbct/job_1/predictions/p.nii.gz", "size": 42},
        )
        # ...and the volume artifact must not claim that row.
        self.assertFalse(self.volume.matches(row))

    def test_bundled_shape_outputs_keyed_in_metadata(self):
        row = self.Row(
            subtype="",
            file_path="maxillo/processed/cbct/job_2/segmentation.nii.gz",
            metadata={
                "files": {
                    "segmentation_nifti": {
                        "path": "maxillo/processed/cbct/job_2/segmentation.nii.gz",
                        "size": 7,
                    }
                }
            },
        )

        self.assertTrue(self.segmentation.matches(row))
        self.assertEqual(self.segmentation.resolve_output(row)["size"], 7)
        self.assertFalse(self.volume.matches(row))

    def test_legacy_shape_volume_plus_pipeline_panoramic(self):
        row = self.Row(
            subtype="processed",
            file_path="processed/cbct/case_pano.png",
            metadata={
                "files": {
                    "volume_nifti": {"path": "processed/cbct/case.nii.gz", "size": 100},
                    "panoramic_view": {"path": "processed/cbct/case_pano.png", "size": 20},
                }
            },
        )

        self.assertTrue(self.volume.matches(row))
        self.assertEqual(self.volume.resolve_output(row)["path"], "processed/cbct/case.nii.gz")
        self.assertTrue(self.pipeline_panoramic.matches(row))
        self.assertEqual(
            self.pipeline_panoramic.resolve_output(row)["path"], "processed/cbct/case_pano.png"
        )
        self.assertFalse(self.segmentation.matches(row))

    def test_the_pipeline_panoramic_lands_in_the_panoramic_folder(self):
        self.assertEqual(self.pipeline_panoramic.zip_directory(), "panoramic/generated")
        self.assertEqual(
            export_catalog.artifact_by_key("maxillo", "panoramic.mip").zip_directory(),
            "panoramic/generated",
        )
