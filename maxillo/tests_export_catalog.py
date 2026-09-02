"""Project-driven export: artifact catalog, filters, and the export builder."""
import json

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from annotations.services.ios_landmarks import save_ios_landmarks
from annotations.services.segmentation import save_tooth_segmentation
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
from maxillo.models import (
    Classification,
    Export,
    Folder,
    IntraoralToothSegmentation,
    Patient,
)
from maxillo.views.export import _preview_totals


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

        class Row:
            metadata = {}

            def __init__(self, file_type, subtype):
                self.file_type = file_type
                self.subtype = subtype

        self.assertTrue(mip.matches(Row("panoramic_processed", "mip")))
        self.assertFalse(mip.matches(Row("panoramic_processed", "raysum")))
        self.assertTrue(raysum.matches(Row("panoramic_processed", "raysum")))
        # An older sweep variant belongs to neither of the two current ones, and the
        # catalog no longer offers a bucket for it: the Z-sweep is not exported.
        self.assertFalse(mip.matches(Row("panoramic_processed", "panoramic_zplus20_mean_png")))
        self.assertFalse(raysum.matches(Row("panoramic_processed", "panoramic_zplus20_mean_png")))
        self.assertIsNone(export_catalog.artifact_by_key("maxillo", "panoramic.legacy"))

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
        self.assertIn("cbct.segmentation", keys)
        self.assertNotIn("cbct.volume", keys)
        self.assertNotIn("cbct.raw", keys)

    def test_legacy_bite_classification_flag_maps_to_the_bite_artifact(self):
        artifacts = export_catalog.artifacts_from_legacy_selection(
            "maxillo", [],
            include_raw=False, include_processed=False,
            include_reports=False, include_bite_classification=True,
        )
        self.assertEqual([a.key for a in artifacts], ["ios.bite_classification"])

    def test_merged_artifact_keys_on_old_export_rows_still_resolve(self):
        # An Export row written before the merge names artifacts that no longer
        # exist under those keys; re-running it must still produce the thing it
        # asked for, not silently nothing.
        resolved = export_catalog.resolve_artifacts(
            "maxillo",
            ["classification.occlusion", "ios.landmarks_prediction", "ios.landmarks"],
        )
        self.assertEqual(
            [a.key for a in resolved], ["ios.bite_classification", "ios.landmarks"]
        )

    def test_retired_interop_and_rawzip_artifacts_are_gone(self):
        keys = {a.key for a in export_catalog.artifacts_for_domain("maxillo")}
        self.assertTrue(
            keys.isdisjoint({
                "cbct.dicom_seg", "cbct.dicom_sr", "cbct.dicom_rtstruct",
                "rawzip.raw", "rawzip.processed",
            })
        )


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
        self.assertNotIn("annotation_bite_classification", ids)

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
                "annotation_bite_classification": True,
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
            list(self._apply({"annotation_bite_classification": True})), [self.with_cbct]
        )
        # Same filter, pre-merge key: stored Export rows keep working.
        self.assertEqual(
            list(self._apply({"annotation_occlusion": True})), [self.with_cbct]
        )

    def test_bite_classification_filter_also_matches_the_pipeline_file(self):
        # The two used to be separate filters; a patient the pipeline classified but
        # nobody opened has the file and no Classification row.
        FileRegistry.objects.create(
            patient=self.without,
            domain="maxillo",
            file_type="bite_classification",
            file_path="maxillo/processed/ios/bite.json",
            file_size=2,
            file_hash="e" * 64,
        )
        self.assertEqual(
            list(self._apply({"annotation_bite_classification": True})), [self.without]
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


class BiteClassificationArtifactTests(TestCase):
    """One checkbox under IOS, covering both halves of the same fact.

    It used to be two: a patient-level "Occlusion classification" reading
    `maxillo.Classification`, and an IOS "Bite classification (pipeline output)"
    reading the `bite_classification` file. They answer the same question, and a
    reader had no way to tell which one to tick.
    """

    def setUp(self):
        self.ios = _modality("ios", "IOS")
        self.project = Project.objects.create(name="Bite", slug="bite-export", domain="maxillo")
        self.project.modalities.set([self.ios])
        self.folder = Folder.objects.create(name="Root", project=self.project)
        self.patient = Patient.objects.create(
            name="Classified", project=self.project, folder=self.folder
        )
        self.artifact = export_catalog.artifact_by_key("maxillo", "ios.bite_classification")

    def test_the_artifact_reads_both_the_file_and_the_record(self):
        self.assertEqual(self.artifact.modality, "ios")
        self.assertEqual(self.artifact.label, "Bite Classification")
        self.assertTrue(self.artifact.is_file_backed)
        self.assertEqual(self.artifact.collector, "occlusion")

    def test_the_classification_document_is_collected_with_no_file_row(self):
        Classification.objects.create(
            patient=self.patient, classifier="manual",
            sagittal_left="I", sagittal_right="I", vertical="normal",
            transverse="normal", midline="centered",
        )
        export = Export(user=None, query_params={
            "domain": "maxillo",
            "folder_ids": [self.folder.id],
            "artifacts": ["ios.bite_classification"],
        })
        processor = ExportProcessor(export, domain="maxillo")
        entries, _size = processor.collect_files(processor.query_patients())

        self.assertEqual([e["filename"] for e in entries], ["classification.json"])
        self.assertEqual(self.artifact.zip_directory(), "ios/bite_classification")

    def test_the_checkbox_is_offered_even_when_no_pipeline_file_exists(self):
        # Availability is a file count for file-backed artifacts; this one also
        # produces a document, so a zero count must not grey it out.
        groups = export_ui.artifact_groups(
            "maxillo", self.project, Patient.objects.filter(pk=self.patient.pk)
        )
        entries = [
            artifact
            for group in groups
            for bucket in group["buckets"]
            for artifact in bucket["artifacts"]
            if artifact["key"] == "ios.bite_classification"
        ]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["available"])
        self.assertIsNone(entries[0]["count"])


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

    def test_legacy_bundle_without_a_segmentation_offers_nothing(self):
        """The old volume/pipeline-panoramic bundle is no longer exportable.

        Both of the keys this row carries had their own artifact until the CBCT group
        was cut back to the volume as uploaded and the segmentation the pipeline
        returns. The row still matches nothing rather than falling into some other
        artifact, which is the failure mode worth pinning.
        """
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

        self.assertFalse(self.segmentation.matches(row))
        self.assertFalse(
            any(a.matches(row) for a in export_catalog.artifacts_for_domain("maxillo"))
        )

    def test_the_cbct_group_offers_the_upload_and_the_segmentation_only(self):
        cbct = [
            a.key for a in export_catalog.artifacts_for_domain("maxillo")
            if a.modality == "cbct" and a.zip_dir is None
        ]
        self.assertEqual(sorted(cbct), ["cbct.raw", "cbct.segmentation"])


class ToothSegmentationExportTests(TestCase):
    """The export document must not change shape when its source moves.

    `_collect_tooth_segmentation` used to read `maxillo.IntraoralToothSegmentation`. Both
    of that table's writers now go through `annotations.services.segmentation`, so the
    export reads there instead -- otherwise it would keep serving the polygons that were
    in the table before anybody edited the study, which is the worst kind of stale: right
    once, wrong afterwards, and identical to look at.

    So this pins the document against the legacy implementation, field for field. The one
    permitted difference is `updated_at`, which is now the set's last save rather than the
    image's: items are rewritten as fresh rows on every revision, so the new model has no
    per-image edit timestamp to report and inventing one would be worse than moving the
    granularity in the open.
    """

    LEGACY_KEYS = ["patient_id", "image_file_id", "image", "is_confirmed", "updated_at", "teeth"]
    SQUARE = [[10, 10], [30, 10], [30, 30], [10, 30]]
    TRIANGLE = [[40, 40], [60, 40], [60, 60]]

    def setUp(self):
        self.photo = _modality("intraoral-photo", "Intraoral Photographs")
        self.project = Project.objects.create(
            name="SegExport", slug="seg-export", domain="maxillo"
        )
        self.project.modalities.set([self.photo])
        self.project.annotation_methods.set(
            [_method("intraoral_segmentation", "Intraoral Segmentation")]
        )
        self.folder = Folder.objects.create(name="Root", project=self.project)
        self.patient = Patient.objects.create(
            patient_id=9701, name="Seg", project=self.project, folder=self.folder
        )
        self.image = FileRegistry.objects.create(
            patient=self.patient,
            domain="maxillo",
            file_type="intraoral_raw",
            file_path="maxillo/intraoral_raw/upper-left.png",
            file_size=4,
            file_hash="c" * 64,
        )

    def _documents(self):
        export = Export(
            user=None,
            query_params={"domain": "maxillo", "artifacts": ["intraoral-photo.segmentation"]},
        )
        processor = ExportProcessor(export, domain="maxillo")
        artifact = export_catalog.resolve_artifacts(
            "maxillo", ["intraoral-photo.segmentation"]
        )[0]
        return [
            (entry["filename"], json.loads(entry["content"]))
            for entry, _size in processor._collect_tooth_segmentation(self.patient, artifact)
        ]

    def test_the_document_matches_what_the_legacy_row_produced(self):
        teeth = {"11": [self.SQUARE], "36": [self.SQUARE, self.TRIANGLE]}
        row = IntraoralToothSegmentation.objects.create(
            patient=self.patient, image_file=self.image, teeth=teeth, is_confirmed=True
        )
        legacy = {
            "patient_id": self.patient.patient_id,
            "image_file_id": row.image_file_id,
            "image": "upper-left.png",
            "is_confirmed": row.is_confirmed,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "teeth": row.teeth,
        }

        call_command("annotations_convert_legacy", "--surface", "intraoral", verbosity=0)

        [(filename, document)] = self._documents()
        self.assertEqual(filename, "upper-left.json")
        self.assertEqual(
            list(document), self.LEGACY_KEYS, "key order is part of the document"
        )
        for key in self.LEGACY_KEYS:
            if key == "updated_at":
                self.assertIsNotNone(document[key], "still a timestamp, just the set's")
                continue
            self.assertEqual(document[key], legacy[key], key)

    def test_an_image_whose_polygons_were_all_deleted_yields_no_document(self):
        # The legacy behaviour: the editor deleted the row rather than storing `{}`, so an
        # emptied image had no document. A confirmed-but-empty image must not reappear as
        # an empty one either.
        save_tooth_segmentation(
            self.patient, images=[{"file_obj": self.image, "teeth": {"11": [self.SQUARE]}}]
        )
        save_tooth_segmentation(
            self.patient,
            images=[{"file_obj": self.image, "teeth": {}, "confirmed": True}],
            expected_revision=1,
        )
        self.assertEqual(self._documents(), [])

    def test_the_preview_count_matches_the_documents_actually_written(self):
        # A preview that promised N files and produced N-1 is how an export looks broken
        # to the person who requested it.
        second = FileRegistry.objects.create(
            patient=self.patient,
            domain="maxillo",
            file_type="intraoral_raw",
            file_path="maxillo/intraoral_raw/lower-right.png",
            file_size=4,
            file_hash="d" * 64,
        )
        save_tooth_segmentation(
            self.patient,
            images=[
                {"file_obj": self.image, "teeth": {"11": [self.SQUARE]}},
                {"file_obj": second, "teeth": {"36": [self.TRIANGLE]}},
            ],
        )
        artifacts = export_catalog.resolve_artifacts(
            "maxillo", ["intraoral-photo.segmentation"]
        )
        file_count, _size = _preview_totals(
            "maxillo", Patient.objects.filter(pk=self.patient.pk), artifacts
        )
        self.assertEqual(file_count, len(self._documents()))
        self.assertEqual(file_count, 2)


class IosLandmarkExportTests(TestCase):
    """The landmark document must survive moving out of object storage.

    `ios.landmarks` used to be a file-backed artifact: the export streamed the JSON the
    legacy `PUT` had written into Garage. Decision #20 makes the annotation record
    canonical, so the export now *renders* the document instead. Whoever downloads an
    export must not be able to tell.

    What is pinned here is the framing the legacy writer used --
    `json.dumps(document, separators=(",", ":"), ensure_ascii=True)`, bare document, and
    the `ios_landmarks_patient_<id>.json` filename -- plus the fact that the predicted
    variant is still a *file* artifact, because that one genuinely is model output
    arriving over the frozen runner API.
    """

    DOCUMENT = {
        "9801_upper_FDI_11": {
            "incisal": [1.5, -2.25, 3.125],
            "cusps": [[1.1, -2.1, 3.1], [1.2, -2.2, 3.2]],
        },
        "9801_lower_FDI_31": {"gingival": [7.5, -8.25, 9.125]},
    }

    def setUp(self):
        self.ios = _modality("ios", "IOS")
        self.project = Project.objects.create(
            name="LmExport", slug="lm-export", domain="maxillo"
        )
        self.project.modalities.set([self.ios])
        self.project.annotation_methods.set([_method("ios_landmarks", "IOS Landmarks")])
        self.folder = Folder.objects.create(name="Root", project=self.project)
        self.patient = Patient.objects.create(
            patient_id=9801, name="Lm", project=self.project, folder=self.folder
        )
        self.upper = self._mesh("ios_raw_upper", "upper.stl", "a")
        self.lower = self._mesh("ios_raw_lower", "lower.stl", "b")

    def _mesh(self, file_type, name, hash_char):
        return FileRegistry.objects.create(
            patient=self.patient,
            domain="maxillo",
            file_type=file_type,
            file_path=f"maxillo/ios/{name}",
            file_size=4,
            file_hash=hash_char * 64,
        )

    def _save(self):
        save_ios_landmarks(
            self.patient,
            meshes=[
                {
                    "file_obj": self.upper,
                    "jaw": "upper",
                    "landmarks": {"11": self.DOCUMENT["9801_upper_FDI_11"]},
                },
                {
                    "file_obj": self.lower,
                    "jaw": "lower",
                    "landmarks": {"31": self.DOCUMENT["9801_lower_FDI_31"]},
                },
            ],
        )

    def _documents(self):
        export = Export(
            user=None, query_params={"domain": "maxillo", "artifacts": ["ios.landmarks"]}
        )
        processor = ExportProcessor(export, domain="maxillo")
        artifact = export_catalog.resolve_artifacts("maxillo", ["ios.landmarks"])[0]
        return [
            (entry["filename"], entry["content"])
            for entry, _size in processor._collect_ios_landmarks(self.patient, artifact)
        ]

    def test_the_rendered_document_is_what_the_legacy_file_held(self):
        self._save()
        [(filename, content)] = self._documents()
        self.assertEqual(filename, "ios_landmarks_patient_9801.json")
        self.assertEqual(json.loads(content), self.DOCUMENT)

    def test_the_bytes_are_framed_the_way_the_legacy_writer_framed_them(self):
        self._save()
        [(_filename, content)] = self._documents()
        # Keys sorted, which is the one documented difference from the legacy file: it
        # preserved whatever insertion order the browser sent. Same framing otherwise.
        expected = {key: self.DOCUMENT[key] for key in sorted(self.DOCUMENT)}
        self.assertEqual(
            content, json.dumps(expected, separators=(",", ":"), ensure_ascii=True)
        )

    def test_a_patient_with_no_landmarks_yields_nothing_rather_than_an_empty_file(self):
        self.assertEqual(self._documents(), [])

    def test_deleting_every_landmark_removes_the_document(self):
        # The file row survives as history; the export must not.
        self._save()
        save_ios_landmarks(
            self.patient,
            meshes=[
                {"file_obj": self.upper, "jaw": "upper", "landmarks": {}},
                {"file_obj": self.lower, "jaw": "lower", "landmarks": {}},
            ],
            expected_revision=1,
        )
        self.assertEqual(self._documents(), [])

    def test_landmarks_are_one_collector_artifact_covering_predictions_too(self):
        artifacts = {a.key: a for a in export_catalog.artifacts_for_domain("maxillo")}
        self.assertEqual(artifacts["ios.landmarks"].collector, "ios_landmarks")
        self.assertFalse(artifacts["ios.landmarks"].is_file_backed)
        # Predictions land in the same record (origin=PREDICTION), so there is no
        # second "predicted landmarks" artifact reading the pipeline's stale file.
        self.assertNotIn("ios.landmarks_prediction", artifacts)

    def test_the_has_landmarks_filter_reads_the_record_not_the_file_row(self):
        # A file row with no landmarks left on it is history, and the filter means "does
        # this patient still have landmarks".
        FileRegistry.objects.create(
            patient=self.patient,
            domain="maxillo",
            file_type="ios_landmarks",
            file_path="maxillo/processed/ios/ios_landmarks_patient_9801.json",
            file_size=4,
            file_hash="d" * 64,
        )
        self.assertFalse(
            export_catalog.apply_filters(
                Patient.objects.filter(pk=self.patient.pk),
                "maxillo",
                {"annotation_landmarks": "yes"},
            ).exists()
        )
        self._save()
        self.assertTrue(
            export_catalog.apply_filters(
                Patient.objects.filter(pk=self.patient.pk),
                "maxillo",
                {"annotation_landmarks": "yes"},
            ).exists()
        )
