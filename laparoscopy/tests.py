import io
import json
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from unittest.mock import patch

import numpy as np
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from common.models import FileRegistry, Modality, Project, ProjectAccess
from common.permissions import filter_folders_for_user, filter_patients_for_user
from laparoscopy.export_processor import LaparoscopyExportProcessor
from laparoscopy.models import Export, Folder, FolderAccess, Patient, RegionAnnotation, RegionType
from maxillo.models import Export as MaxilloExport


@contextmanager
def _yield_path(path, suffix=""):
    del suffix
    yield path


class _FakeStorage:
    def __init__(self):
        fd, self.uploaded_path = tempfile.mkstemp(prefix="lap_export_", suffix=".zip")
        self.uploaded_key = None
        self.last_metadata = None
        self._closed = False
        self._fd = fd
        self.close()

    def upload_file(self, local_path, *, key, content_type=None, metadata=None):
        del content_type
        self.uploaded_key = key
        self.last_metadata = metadata or {}
        shutil.copyfile(local_path, self.uploaded_path)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._fd is not None:
                import os

                os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def cleanup(self):
        self.close()
        try:
            import os

            os.remove(self.uploaded_path)
        except OSError:
            pass


class _MemoryStorage:
    """Object storage that keeps what was written, so a test can read it back."""

    def __init__(self):
        self.objects = {}

    def upload_fileobj(self, fileobj, *, key, content_type=None, metadata=None):
        self.objects[key] = fileobj.read()
        return None


class LaparoscopyExportBaseTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="lap-admin", password="pw")
        self.project = Project.objects.get_or_create(
            slug="laparoscopy", defaults={"name": "Laparoscopy", "domain": "laparoscopy"}
        )[0]
        self.video_modality = Modality.objects.create(name="Video", slug="video")
        self.project.modalities.add(self.video_modality)
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.folder = Folder.objects.create(name="Case Batch", project=self.project)

    def create_patient(self, name="Patient", folder=None):
        folder = folder or self.folder
        return Patient.objects.create(name=name, folder=folder, project=folder.project)

    def create_subsampled_video(self, patient, file_path=None, file_size=128):
        return FileRegistry.objects.create(
            domain="laparoscopy",
            file_type="video_processed",
            subtype="subsampled",
            modality=self.video_modality,
            laparoscopy_patient=patient,
            file_path=file_path or f"laparoscopy/patient_{patient.patient_id}/subsampled.mp4",
            file_size=file_size,
            file_hash=f"hash-{patient.patient_id}-{file_size}",
        )

    def create_export(self):
        return Export.objects.create(
            user=self.user,
            status="pending",
            query_params={
                "domain": "laparoscopy",
                "export_variant": "video_masks_v1",
                "folder_ids": [self.folder.id],
                "mask_format": "npz_multilayer",
                "include_all_frames": True,
                "video_subtype": "subsampled",
            },
            query_summary="laparoscopy export",
        )

    def create_region_type(self, name, order):
        return RegionType.objects.create(
            project=self.project,
            name=name,
            color="#00aa00" if order == 0 else "#aa0000",
            order=order,
        )


class LaparoscopyExportViewTests(LaparoscopyExportBaseTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_export_preview_counts_only_patients_with_subsampled_video(self):
        self.create_subsampled_video(self.create_patient(name="Exportable"), file_size=321)
        self.create_patient(name="MissingVideo")

        response = self.client.post(
            reverse("laparoscopy:export_preview"),
            data=json.dumps({"folder_ids": [self.folder.id]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["patient_count"], 2)
        self.assertEqual(payload["exportable_patient_count"], 1)
        self.assertEqual(payload["file_count"], 1)
        self.assertEqual(payload["estimated_size_bytes"], 321)

    def test_export_list_is_filtered_to_laparoscopy_exports(self):
        lap_export = Export.objects.create(
            user=self.user,
            status="pending",
            query_params={"folder_ids": [self.folder.id]},
            query_summary="lap",
        )
        MaxilloExport.objects.create(
            user=self.user,
            status="pending",
            query_params={"folder_ids": [999]},
            query_summary="maxillo",
        )

        response = self.client.get(reverse("laparoscopy:export_list"))

        self.assertEqual(response.status_code, 200)
        exports = [item["export"].id for item in response.context["page_obj"].object_list]
        self.assertEqual(exports, [lap_export.id])


class LaparoscopyExportProcessorTests(LaparoscopyExportBaseTestCase):
    def _run_processor(self, export, local_video_path, probe_result):
        storage = _FakeStorage()
        self.addCleanup(storage.cleanup)

        with patch("laparoscopy.export_processor.download_to_tempfile", side_effect=lambda key, suffix="": _yield_path(local_video_path, suffix)):
            with patch("laparoscopy.export_processor.get_object_storage", return_value=storage):
                with patch.object(LaparoscopyExportProcessor, "_probe_video", return_value=probe_result):
                    processor = LaparoscopyExportProcessor(export)
                    processor.process_export()

        export.refresh_from_db()
        self.assertEqual(export.status, "completed")
        return storage.uploaded_path

    def test_processor_exports_all_frames_and_preserves_overlaps(self):
        patient = self.create_patient(name="Overlap Case")
        self.create_subsampled_video(patient)
        region_a = self.create_region_type("Region A", 0)
        region_b = self.create_region_type("Region B", 1)
        RegionAnnotation.objects.create(
            patient=patient,
            region_type=region_a,
            tool="polygon",
            frame_time=0.0,
            points=[1, 1, 6, 1, 6, 6, 1, 6],
            stroke_width=1.0,
            created_by=self.user,
            updated_by=self.user,
        )
        RegionAnnotation.objects.create(
            patient=patient,
            region_type=region_b,
            tool="polygon",
            frame_time=0.0,
            points=[2, 2, 5, 2, 5, 5, 2, 5],
            stroke_width=1.0,
            created_by=self.user,
            updated_by=self.user,
        )
        export = self.create_export()

        with tempfile.NamedTemporaryFile(suffix=".mp4") as video_file:
            video_file.write(b"fake-video")
            video_file.flush()
            uploaded_zip_path = self._run_processor(
                export,
                local_video_path=video_file.name,
                probe_result={"width": 8, "height": 8, "fps": 1.0, "frame_count": 2},
            )

        with zipfile.ZipFile(uploaded_zip_path) as zipf:
            manifest = json.loads(zipf.read("manifest.json").decode("utf-8"))
            patient_folder = manifest["patients"][0]["zip_video_path"].split("/video/")[0]

            with np.load(io.BytesIO(zipf.read(f"{patient_folder}/masks/frame_000000.npz"))) as frame_zero:
                masks_zero = frame_zero["masks"]
            with np.load(io.BytesIO(zipf.read(f"{patient_folder}/masks/frame_000001.npz"))) as frame_one:
                masks_one = frame_one["masks"]

        self.assertEqual(masks_zero.shape, (2, 8, 8))
        self.assertEqual(int(masks_zero[0, 3, 3]), 1)
        self.assertEqual(int(masks_zero[1, 3, 3]), 1)
        self.assertEqual(int(masks_one.sum()), 0)

    def test_processor_eraser_only_clears_its_own_layer(self):
        patient = self.create_patient(name="Eraser Case")
        self.create_subsampled_video(patient)
        region_a = self.create_region_type("Region A", 0)
        region_b = self.create_region_type("Region B", 1)
        RegionAnnotation.objects.create(
            patient=patient,
            region_type=region_a,
            tool="polygon",
            frame_time=0.0,
            points=[1, 1, 6, 1, 6, 6, 1, 6],
            stroke_width=1.0,
            created_by=self.user,
            updated_by=self.user,
        )
        RegionAnnotation.objects.create(
            patient=patient,
            region_type=region_b,
            tool="polygon",
            frame_time=0.0,
            points=[1, 1, 6, 1, 6, 6, 1, 6],
            stroke_width=1.0,
            created_by=self.user,
            updated_by=self.user,
        )
        RegionAnnotation.objects.create(
            patient=patient,
            region_type=region_a,
            tool="eraser",
            frame_time=0.0,
            points=[1, 4, 6, 4],
            stroke_width=3.0,
            created_by=self.user,
            updated_by=self.user,
        )
        export = self.create_export()

        with tempfile.NamedTemporaryFile(suffix=".mp4") as video_file:
            video_file.write(b"fake-video")
            video_file.flush()
            uploaded_zip_path = self._run_processor(
                export,
                local_video_path=video_file.name,
                probe_result={"width": 8, "height": 8, "fps": 1.0, "frame_count": 1},
            )

        with zipfile.ZipFile(uploaded_zip_path) as zipf:
            manifest = json.loads(zipf.read("manifest.json").decode("utf-8"))
            patient_folder = manifest["patients"][0]["zip_video_path"].split("/video/")[0]
            with np.load(io.BytesIO(zipf.read(f"{patient_folder}/masks/frame_000000.npz"))) as frame_zero:
                masks_zero = frame_zero["masks"]

        self.assertEqual(int(masks_zero[0, 4, 4]), 0)
        self.assertEqual(int(masks_zero[1, 4, 4]), 1)

    def test_masks_from_a_labelmap_are_the_masks_from_the_strokes(self):
        """Risk 18, decision #15: regenerated from labelmaps, byte-for-byte the same.

        The same study is exported twice -- once with only legacy strokes, so the
        processor rasterises them, and once after `annotations_rasterize_video_masks`
        has turned those strokes into a stored labelmap, so the processor reads the
        labelmap instead. Every frame's array is compared.

        They agree by construction rather than by coincidence: the command and the
        export both rasterise through `laparoscopy.mask_raster`, replaying the strokes
        in `created_at` order, which matters because the eraser is destructive and a
        different order paints back what a later stroke removed.

        **This is proof on a fixture, not on production studies.** Risk 18 asks for the
        latter and it stays open: nothing here has seen a real operation.
        """
        patient = self.create_patient(name="Equivalence Case")
        self.create_subsampled_video(patient)
        raw_video = FileRegistry.objects.create(
            domain="laparoscopy",
            file_type="video_raw",
            modality=self.video_modality,
            laparoscopy_patient=patient,
            file_path=f"laparoscopy/patient_{patient.patient_id}/raw.mp4",
            file_size=64,
            file_hash=f"rawhash-{patient.patient_id}",
        )
        region_a = self.create_region_type("Region A", 0)
        region_b = self.create_region_type("Region B", 1)
        for region, tool, points, width in (
            (region_a, "polygon", [1, 1, 6, 1, 6, 6, 1, 6], 1.0),
            (region_b, "polygon", [2, 2, 5, 2, 5, 5, 2, 5], 1.0),
            (region_a, "eraser", [1, 4, 6, 4], 3.0),
        ):
            RegionAnnotation.objects.create(
                patient=patient, region_type=region, tool=tool, frame_time=0.0,
                points=points, stroke_width=width,
                created_by=self.user, updated_by=self.user,
            )

        probe = {"width": 8, "height": 8, "fps": 1.0, "frame_count": 2}
        from_strokes = self._export_masks(patient, probe)

        storage = _MemoryStorage()
        with patch("common.object_storage.get_object_storage", return_value=storage):
            with patch(
                "common.file_access.open_binary",
                side_effect=lambda key: (io.BytesIO(storage.objects[key]), None),
            ):
                with patch(
                    "laparoscopy.video_probe.probe_video", return_value=probe
                ):
                    with patch(
                        "common.object_storage.download_to_tempfile",
                        side_effect=lambda key, suffix="": _yield_path(__file__, suffix),
                    ):
                        call_command(
                            "annotations_rasterize_video_masks",
                            "--patient", str(patient.patient_id),
                            verbosity=0,
                        )
                from_labelmap = self._export_masks(patient, probe)

        self.assertEqual(sorted(from_strokes), sorted(from_labelmap))
        for name in from_strokes:
            np.testing.assert_array_equal(
                from_strokes[name], from_labelmap[name],
                err_msg=f"{name} differs between the stroke and labelmap paths",
            )
        # And it is not vacuously equal: the eraser really did clear its own layer.
        self.assertEqual(int(from_labelmap["frame_000000.npz"][0, 4, 4]), 0)
        self.assertEqual(int(from_labelmap["frame_000000.npz"][1, 4, 4]), 1)

    def _export_masks(self, patient, probe):
        """Run one export and return ``{frame filename: masks array}``."""
        export = self.create_export()
        with tempfile.NamedTemporaryFile(suffix=".mp4") as video_file:
            video_file.write(b"fake-video")
            video_file.flush()
            uploaded_zip_path = self._run_processor(
                export, local_video_path=video_file.name, probe_result=probe
            )
        masks = {}
        with zipfile.ZipFile(uploaded_zip_path) as zipf:
            manifest = json.loads(zipf.read("manifest.json").decode("utf-8"))
            folder = manifest["patients"][0]["zip_video_path"].split("/video/")[0]
            for name in zipf.namelist():
                if not name.startswith(f"{folder}/masks/"):
                    continue
                with np.load(io.BytesIO(zipf.read(name))) as archive:
                    masks[name.rsplit("/", 1)[-1]] = archive["masks"]
        return masks

    def test_run_export_command_accepts_laparoscopy_domain(self):
        export = self.create_export()

        with patch("maxillo.management.commands.run_export.LaparoscopyExportProcessor") as processor_cls:
            processor_cls.return_value.process_export.return_value = None
            call_command("run_export", export.id, "--domain", "laparoscopy")

        processor_cls.assert_called_once()
        processor_cls.return_value.process_export.assert_called_once_with()


class LaparoscopyProjectAccessTests(TestCase):
    """Project-level ACL for laparoscopy (mirrors maxillo/brain behavior)."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.get_or_create(
            slug="laparoscopy", defaults={"name": "Laparoscopy", "domain": "laparoscopy"}
        )[0]
        self.admin = User.objects.create_user(username="lap-acl-admin", password="pw")
        ProjectAccess.objects.create(user=self.admin, project=self.project, role="admin")
        self.member = User.objects.create_user(username="lap-acl-member", password="pw")
        ProjectAccess.objects.create(user=self.member, project=self.project, role="viewer")
        self.outsider = User.objects.create_user(username="lap-acl-outsider", password="pw")

        self.folder_a = Folder.objects.create(name="Folder A", project=self.project)
        self.folder_b = Folder.objects.create(name="Folder B", project=self.project)
        self.patient_a = Patient.objects.create(name="PA", folder=self.folder_a, project=self.project)
        self.patient_b = Patient.objects.create(name="PB", folder=self.folder_b, project=self.project)

    def test_member_without_project_access_sees_no_folders(self):
        qs = filter_folders_for_user(self.outsider, Folder.objects.all(), "laparoscopy")
        self.assertEqual(qs.count(), 0)

    def test_member_without_project_access_sees_no_patients(self):
        qs = filter_patients_for_user(self.outsider, Patient.objects.all(), "laparoscopy")
        self.assertEqual(qs.count(), 0)

    def test_project_member_sees_folders(self):
        qs = filter_folders_for_user(self.member, Folder.objects.all(), "laparoscopy")
        self.assertIn(self.folder_a, qs)
        self.assertIn(self.folder_b, qs)

    def test_project_member_sees_patients(self):
        qs = filter_patients_for_user(self.member, Patient.objects.all(), "laparoscopy")
        self.assertEqual(qs.count(), 2)

    def test_project_admin_sees_all_folders(self):
        qs = filter_folders_for_user(self.admin, Folder.objects.all(), "laparoscopy")
        self.assertIn(self.folder_a, qs)
        self.assertIn(self.folder_b, qs)

    def test_project_admin_sees_all_patients(self):
        qs = filter_patients_for_user(self.admin, Patient.objects.all(), "laparoscopy")
        self.assertEqual(qs.count(), 2)


class LaparoscopyRegionTypeAccessTests(TestCase):
    """Who may edit the project's region vocabulary.

    Naming what is drawn is annotation work: an annotator who finds a type missing or
    misnamed fixes it. A viewer reads only.
    """

    def setUp(self):
        super().setUp()
        self.project = Project.objects.get_or_create(
            slug="laparoscopy", defaults={"name": "Laparoscopy", "domain": "laparoscopy"}
        )[0]
        self.annotator = User.objects.create_user(username="lap-rt-annotator", password="pw")
        ProjectAccess.objects.create(user=self.annotator, project=self.project, role="annotator")
        self.viewer = User.objects.create_user(username="lap-rt-viewer", password="pw")
        ProjectAccess.objects.create(user=self.viewer, project=self.project, role="viewer")

    def _type(self, name="Liver"):
        return RegionType.objects.create(project=self.project, name=name, color="#3498db", order=0)

    def test_annotator_creates_region_type(self):
        self.client.force_login(self.annotator)
        response = self.client.post(
            "/laparoscopy/api/region-types/",
            data=json.dumps({"name": "Gallbladder"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(RegionType.objects.filter(project=self.project, name="Gallbladder").exists())

    def test_annotator_renames_region_type(self):
        region = self._type()
        self.client.force_login(self.annotator)
        response = self.client.patch(
            f"/laparoscopy/api/region-types/{region.id}/",
            data=json.dumps({"name": "Liver segment"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        region.refresh_from_db()
        self.assertEqual(region.name, "Liver segment")

    def test_annotator_deletes_region_type(self):
        region = self._type()
        self.client.force_login(self.annotator)
        response = self.client.delete(f"/laparoscopy/api/region-types/{region.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(RegionType.objects.filter(pk=region.pk).exists())

    def test_viewer_cannot_create_rename_or_delete(self):
        region = self._type()
        self.client.force_login(self.viewer)
        self.assertEqual(
            self.client.post(
                "/laparoscopy/api/region-types/",
                data=json.dumps({"name": "Gallbladder"}),
                content_type="application/json",
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.patch(
                f"/laparoscopy/api/region-types/{region.id}/",
                data=json.dumps({"name": "Nope"}),
                content_type="application/json",
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(f"/laparoscopy/api/region-types/{region.id}/").status_code, 403
        )
        region.refresh_from_db()
        self.assertEqual(region.name, "Liver")

    def test_viewer_still_recolours_their_own_view(self):
        # A per-user colour is a preference, not a change to the project's vocabulary.
        region = self._type()
        self.client.force_login(self.viewer)
        response = self.client.patch(
            f"/laparoscopy/api/region-types/{region.id}/",
            data=json.dumps({"color": "#123456"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["color"], "#123456")
