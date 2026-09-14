from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, Client
from django.urls import reverse

import os
from io import BytesIO
from unittest.mock import MagicMock, patch

from common.models import FileRegistry, Modality, Project, ProjectAccess
from urology.models import Folder, Patient, Tag, UrologyProject


class UrologyDomainTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser(
            username="urology_admin",
            email="urology_admin@example.com",
            password="password123",
        )
        call_command("setup_urology_modalities")
        self.project = Project.objects.get(slug="urology")
        ProjectAccess.objects.get_or_create(
            user=self.user, project=self.project, defaults={"role": "admin"}
        )
        self.folder = Folder.objects.create(
            name="Test Folder", project=self.project, created_by=self.user
        )
        self.patient = Patient.objects.create(
            name="Test Patient 1",
            folder=self.folder,
            project=self.project,
            uploaded_by=self.user,
        )
        self.client.login(username="urology_admin", password="password123")

    def test_setup_urology_modalities(self):
        self.assertEqual(self.project.modalities.count(), 2)
        slugs = set(self.project.modalities.values_list("slug", flat=True))
        self.assertIn("urology-mri", slugs)
        self.assertIn("urology-wsi", slugs)

    def test_urology_root_redirect(self):
        response = self.client.get("/urology/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/urology/patients/", response.headers["Location"])

    def test_urology_patient_list_view(self):
        response = self.client.get("/urology/patients/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Patient 1")
        self.assertContains(response, "Urology MRI")
        self.assertContains(response, "Urology WSI")

    def test_urology_filter_by_folder(self):
        response = self.client.get(f"/urology/patients/?folder={self.folder.id}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Patient 1")

    def test_urology_upload_get(self):
        upload_resp = self.client.get("/urology/upload/")
        self.assertEqual(upload_resp.status_code, 200)
        self.assertContains(upload_resp, "Upload patient data")
        self.assertContains(upload_resp, "Urology MRI")
        self.assertContains(upload_resp, "Digital Pathology WSI")

    def test_urology_upload_post(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from common.models import FileRegistry, Job

        mri_file = SimpleUploadedFile("mri_scan.nii.gz", b"FAKE_NIFTI_DATA", content_type="application/gzip")
        wsi_file = SimpleUploadedFile("histology.tiff", b"FAKE_TIFF_DATA", content_type="image/tiff")

        post_data = {
            "name": "Uploaded Urology Patient",
            "project": self.project.id,
            "folder": self.folder.id,
            "tags_text": "upload-test, biopsy",
            "urology-mri": mri_file,
            "urology-wsi": wsi_file,
        }

        resp = self.client.post("/urology/upload/", data=post_data, follow=True)
        self.assertEqual(resp.status_code, 200)

        patient = Patient.objects.filter(name="Uploaded Urology Patient").first()
        self.assertIsNotNone(patient)
        self.assertEqual(patient.folder, self.folder)
        self.assertIn("biopsy", patient.tag_names())

        files = FileRegistry.objects.filter(domain="urology", urology_patient=patient)
        self.assertEqual(files.count(), 2)
        mri_fr = files.filter(modality__slug="urology-mri").first()
        wsi_fr = files.filter(modality__slug="urology-wsi").first()
        self.assertIsNotNone(mri_fr)
        self.assertIsNotNone(wsi_fr)
        self.assertEqual(mri_fr.file_type, "urology_mri_raw")
        self.assertEqual(wsi_fr.file_type, "urology_wsi_raw")

        jobs = Job.objects.filter(domain="urology", urology_patient=patient)
        self.assertEqual(jobs.count(), 2)

    def test_urology_patient_detail(self):
        detail_resp = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(detail_resp.status_code, 200)
        self.assertContains(detail_resp, self.patient.name)
        self.assertContains(detail_resp, f"ID {self.patient.patient_id}")
        self.assertContains(detail_resp, "urology-multimodal-bar")
        self.assertContains(detail_resp, "urologyMriStage")
        self.assertContains(detail_resp, "urologyWsiStagePanel")

        # Verify Captions menu/tab is active by default instead of Files
        self.assertContains(detail_resp, 'class="side-tab is-active" data-tab-target="captions"')
        self.assertContains(detail_resp, 'class="side-tab-pane is-active" data-tab-pane="captions"')
        self.assertNotContains(detail_resp, 'class="side-tab is-active" data-tab-target="files"')
        self.assertNotContains(detail_resp, 'class="side-tab-pane is-active" data-tab-pane="files"')

    def test_urology_report_template_voices(self):
        detail_resp = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(detail_resp.status_code, 200)

        # Verify key Italian Urology report voices are rendered
        self.assertContains(detail_resp, "Volume Prostatico e Densità del PSA")
        self.assertContains(detail_resp, "Sede e Settore della Lesione Indice")
        self.assertContains(detail_resp, "Dimensioni della Lesione")
        self.assertContains(detail_resp, "Punteggio PI-RADS")
        self.assertContains(detail_resp, "Intensità T2 e Restrizione in Diffusione (ADC)")
        self.assertContains(detail_resp, "Impregnazione di Contrasto Dinamica")
        self.assertContains(detail_resp, "Estensione Extracapsulare")
        self.assertContains(detail_resp, "Invasione delle Vescicole Seminali")
        self.assertContains(detail_resp, "Rapporti con Fasci Neurovascolari e Collo Vescicale")
        self.assertContains(detail_resp, "Linfonodi Regionali e Scheletro del Bacino")
        self.assertContains(detail_resp, "Istotipo Tumorale")
        self.assertContains(detail_resp, "Gleason Score e Grade Group ISUP")
        self.assertContains(detail_resp, "Percentuale Pattern 4 o 5 e Architettura Cribriforme")
        self.assertContains(detail_resp, "Carcinoma Intraduttale")
        self.assertContains(detail_resp, "Invasione Perineurale e Vascolare")
        self.assertContains(detail_resp, "Estensione Tumorale nel Prelievo e Margini Chirurgici")
        self.assertContains(detail_resp, "Parenchima Non Tumorale e Lesioni Concomitanti")
        self.assertContains(detail_resp, "Correlazione Radio-Patologica e Reperti Conclusivi")

        # Verify no repeated acronym clutter in report section
        self.assertNotContains(detail_resp, "(RM)")
        self.assertNotContains(detail_resp, "(WSI)")

        # Verify other domain templates are not rendered in urology
        self.assertNotContains(detail_resp, "Classe Scheletrica")
        self.assertNotContains(detail_resp, "Overjet")
        self.assertNotContains(detail_resp, "Invasione Ependimale")

    @patch("urology.wsi_views.open_binary")
    @patch("urology.wsi_views.get_wsi_metadata")
    def test_urology_wsi_metadata(self, mock_get_metadata, mock_open_binary):
        mock_open_binary.return_value = (BytesIO(b"dummy_tiff"), MagicMock())
        mock_get_metadata.return_value = {
            "width": 1024,
            "height": 1024,
            "tileWidth": 256,
            "tileHeight": 256,
            "levels": [{"level": 0, "width": 1024, "height": 1024, "downsample": 1.0, "cols": 4, "rows": 4}],
            "mpp": 0.25,
            "magnification": 40.0,
            "vendor": "generic",
        }
        wsi_modality = Modality.objects.get(slug="urology-wsi")
        fr = FileRegistry.objects.create(
            domain="urology",
            urology_patient=self.patient,
            modality=wsi_modality,
            file_type="urology_wsi_raw",
            file_path="urology/test.tiff",
            file_size=1024,
            file_hash="hash123",
            metadata={"original_filename": "test.tiff"},
        )
        resp = self.client.get(f"/urology/api/wsi/{fr.id}/metadata/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["fileId"], fr.id)
        self.assertEqual(data["width"], 1024)
        self.assertEqual(data["mpp"], 0.25)

    @patch("urology.wsi_views.open_binary")
    @patch("urology.wsi_views.get_wsi_tile")
    def test_urology_wsi_tile(self, mock_get_tile, mock_open_binary):
        mock_open_binary.return_value = (BytesIO(b"dummy_tiff"), MagicMock())
        mock_get_tile.return_value = b"\x89PNG\r\n\x1a\nfake_png"
        wsi_modality = Modality.objects.get(slug="urology-wsi")
        fr = FileRegistry.objects.create(
            domain="urology",
            urology_patient=self.patient,
            modality=wsi_modality,
            file_type="urology_wsi_raw",
            file_path="urology/test_tile.tiff",
            file_size=1024,
            file_hash="hash_tile",
            metadata={"original_filename": "test_tile.tiff"},
        )
        resp = self.client.get(f"/urology/api/wsi/{fr.id}/tile/0/1_2.png")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertEqual(resp.content, b"\x89PNG\r\n\x1a\nfake_png")

        # Test If-None-Match 304 caching
        etag = f'"{fr.file_hash}_0_1_2"'
        resp_cached = self.client.get(
            f"/urology/api/wsi/{fr.id}/tile/0/1_2.png",
            HTTP_IF_NONE_MATCH=etag,
        )
        self.assertEqual(resp_cached.status_code, 304)

    @patch("urology.wsi_views.open_binary")
    @patch("urology.wsi_views.get_wsi_thumbnail")
    def test_urology_wsi_thumbnail(self, mock_get_thumb, mock_open_binary):
        mock_open_binary.return_value = (BytesIO(b"dummy_tiff"), MagicMock())
        mock_get_thumb.return_value = b"\xff\xd8\xfffake_jpg"
        wsi_modality = Modality.objects.get(slug="urology-wsi")
        fr = FileRegistry.objects.create(
            domain="urology",
            urology_patient=self.patient,
            modality=wsi_modality,
            file_type="urology_wsi_raw",
            file_path="urology/test_thumb.tiff",
            file_size=1024,
            file_hash="hash_thumb",
            metadata={"original_filename": "test_thumb.tiff"},
        )
        resp = self.client.get(f"/urology/api/wsi/{fr.id}/thumbnail/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/jpeg")

    def test_urology_wsi_disk_and_frame_cache(self):
        """Verify on-disk tile cache and in-memory frame cache functionality."""
        from urology.wsi_views import WSI_TILES_DIR, _TILE_CACHE, _TILE_CACHE_LOCK
        from urology.wsi_reader import _FRAME_CACHE, _FRAME_CACHE_LOCK, get_wsi_tile
        from PIL import Image

        # Create a small synthetic image for test
        img = Image.new("RGB", (512, 512), (120, 150, 180))
        buf = BytesIO()
        img.save(buf, format="TIFF")
        buf.seek(0)

        # 1. Test get_wsi_tile with slide_key caches frame
        slide_key = "test_slide_key_123"
        tile1 = get_wsi_tile(buf, level=0, col=0, row=0, tile_size=256, slide_key=slide_key)
        self.assertGreater(len(tile1), 0)

        with _FRAME_CACHE_LOCK:
            self.assertIn((slide_key, 0), _FRAME_CACHE)

        # Slicing next tile uses frame cache
        tile2 = get_wsi_tile(buf, level=0, col=1, row=0, tile_size=256, slide_key=slide_key)
        self.assertGreater(len(tile2), 0)

        # 2. Test disk tile cache in wsi_tile_api
        wsi_modality = Modality.objects.get(slug="urology-wsi")
        fr = FileRegistry.objects.create(
            domain="urology",
            urology_patient=self.patient,
            modality=wsi_modality,
            file_type="urology_wsi_raw",
            file_path="urology/disk_test.tiff",
            file_size=1024,
            file_hash="test_disk_hash",
            metadata={"original_filename": "disk_test.tiff"},
        )
        slide_dir = os.path.join(WSI_TILES_DIR, fr.file_hash)
        os.makedirs(slide_dir, exist_ok=True)
        disk_tile_path = os.path.join(slide_dir, "0_0_0.jpg")
        with open(disk_tile_path, "wb") as f:
            f.write(b"cached_disk_jpeg_data")

        # Clear in-memory tile cache to force disk hit
        with _TILE_CACHE_LOCK:
            _TILE_CACHE.clear()

        resp = self.client.get(f"/urology/api/wsi/{fr.id}/tile/0/0_0.jpg")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"cached_disk_jpeg_data")

    def test_urology_measurements_state_and_save(self):
        # 1. State should be empty initially
        state_resp = self.client.get(f"/urology/api/patients/{self.patient.patient_id}/measurements/state/")
        self.assertEqual(state_resp.status_code, 200)
        state_data = state_resp.json()
        self.assertEqual(state_data["revision"], 0)
        self.assertEqual(state_data["annotations"], [])

        # 2. Save measurement
        wsi_modality = Modality.objects.get(slug="urology-wsi")
        fr = FileRegistry.objects.create(
            domain="urology",
            urology_patient=self.patient,
            modality=wsi_modality,
            file_type="urology_wsi_raw",
            file_path="urology/test_save.tiff",
            file_size=1024,
            file_hash="hash_save",
        )
        import json
        save_payload = {
            "fileId": fr.id,
            "expectedRevision": 0,
            "coordinateSystem": "image_pixel",
            "volumeDescriptor": {
                "wsi": True,
                "mpp": 0.25,
            },
            "annotations": [
                {
                    "annotationUID": "test-annotation-1",
                    "metadata": {
                        "toolName": "Length",
                    },
                    "data": {
                        "handles": {
                            "points": [
                                [100.0, 100.0],
                                [200.0, 100.0],
                            ]
                        }
                    },
                }
            ],
        }
        save_resp = self.client.post(
            f"/urology/api/patients/{self.patient.patient_id}/measurements/",
            data=json.dumps(save_payload),
            content_type="application/json",
        )
        self.assertEqual(save_resp.status_code, 200)
        save_result = save_resp.json()
        self.assertEqual(save_result["revision"], 1)

        # 3. Verify state returns revision 1 and the saved annotation
        state_resp2 = self.client.get(f"/urology/api/patients/{self.patient.patient_id}/measurements/state/")
        self.assertEqual(state_resp2.status_code, 200)
        state_data2 = state_resp2.json()
        self.assertEqual(state_data2["revision"], 1)
        self.assertEqual(len(state_data2["annotations"]), 1)
        self.assertEqual(state_data2["annotations"][0]["metadata"]["toolName"], "Length")

    def test_urology_folder_operations(self):
        create_resp = self.client.post(
            "/urology/folders/create/",
            data='{"name": "New Urology Folder"}',
            content_type="application/json",
        )
        self.assertEqual(create_resp.status_code, 200)
        self.assertTrue(Folder.objects.filter(name="New Urology Folder").exists())

    def test_urology_tag_operations(self):
        add_tag_resp = self.client.post(
            f"/urology/patient/{self.patient.patient_id}/tags/add/",
            data='{"tag": "biopsy-confirmed"}',
            content_type="application/json",
        )
        self.assertEqual(add_tag_resp.status_code, 200)
        self.assertIn("biopsy-confirmed", self.patient.tag_names())

    def test_urology_export_views(self):
        resp = self.client.get("/urology/export/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Urology Export Datasets")

        # Verify fallback to active urology project when session is unset or cross-domain
        session = self.client.session
        if "current_project_id" in session:
            del session["current_project_id"]
        session.save()

        new_resp = self.client.get("/urology/export/new/")
        self.assertEqual(new_resp.status_code, 200)
        self.assertContains(new_resp, "New Export - Urology")
        self.assertContains(new_resp, self.project.name)

        # Test export preview endpoint
        preview_resp = self.client.post(
            "/urology/export/preview/",
            data=f'{{"folder_ids": [{self.folder.id}], "artifacts": ["urology-mri.raw", "urology-wsi.raw"]}}',
            content_type="application/json",
        )
        self.assertEqual(preview_resp.status_code, 200)
        preview_data = preview_resp.json()
        self.assertTrue(preview_data["success"])
        self.assertEqual(preview_data["folder_count"], 1)

        # Test creating an export
        post_data = {
            "folder_ids": [str(self.folder.id)],
            "artifacts": ["urology-mri.raw", "urology-wsi.raw"],
        }
        create_resp = self.client.post("/urology/export/new/", data=post_data, follow=True)
        self.assertEqual(create_resp.status_code, 200)
        from urology.models import Export
        export = Export.objects.filter(user=self.user).first()
        self.assertIsNotNone(export)
        self.assertIn("urology-wsi.raw", export.query_params.get("artifacts", []))

        # Test export status
        status_resp = self.client.get(f"/urology/export/{export.id}/")
        self.assertEqual(status_resp.status_code, 200)
        self.assertEqual(status_resp.json()["id"], export.id)

        # Test sharing update
        export.status = "completed"
        export.file_path = "urology/exports/test_export.zip"
        export.file_size = 2048
        export.save()
        share_resp = self.client.post(
            f"/urology/export/{export.id}/share/",
            data='{"share_mode": "public", "expires_in_days": 7}',
            content_type="application/json",
        )
        self.assertEqual(share_resp.status_code, 200)
        self.assertTrue(share_resp.json()["success"])
        export.refresh_from_db()
        self.assertEqual(export.share_mode, "public")
        self.assertIsNotNone(export.share_token)

        # Test shared landing page
        with patch("urology.views.artifact_exists", return_value=True):
            landing_resp = self.client.get(f"/urology/export/shared/{export.share_token}/")
            self.assertEqual(landing_resp.status_code, 200)
            self.assertContains(landing_resp, "Shared Urology Dataset")

        # Test export delete
        del_resp = self.client.post(f"/urology/export/{export.id}/delete/")
        self.assertEqual(del_resp.status_code, 200)
        self.assertFalse(Export.objects.filter(id=export.id).exists())

    def test_urology_caption_endpoints(self):
        # 1. Create text caption
        import json
        caption_resp = self.client.post(
            f"/urology/patient/{self.patient.patient_id}/text-caption/",
            data=json.dumps({"text": "Prostate tumor observed in peripheral zone.", "modality": "urology-mri"}),
            content_type="application/json",
        )
        self.assertEqual(caption_resp.status_code, 200)
        c_data = caption_resp.json()
        self.assertTrue(c_data["success"])
        caption_id = c_data["caption"]["id"]
        self.assertEqual(c_data["caption"]["text_caption"], "Prostate tumor observed in peripheral zone.")

        # 2. Update modality
        update_mod_resp = self.client.post(
            f"/urology/patient/{self.patient.patient_id}/voice-caption/{caption_id}/update-modality/",
            data=json.dumps({"modality": "urology-wsi"}),
            content_type="application/json",
        )
        self.assertEqual(update_mod_resp.status_code, 200)
        self.assertTrue(update_mod_resp.json()["success"])

        # 3. Edit transcription
        edit_resp = self.client.post(
            f"/urology/patient/{self.patient.patient_id}/voice-caption/{caption_id}/edit/",
            data=json.dumps({"action": "edit", "text": "Confirmed Gleason 4+3 lesion."}),
            content_type="application/json",
        )
        self.assertEqual(edit_resp.status_code, 200)
        self.assertEqual(edit_resp.json()["caption"]["text_caption"], "Confirmed Gleason 4+3 lesion.")

        # 4. Delete caption
        del_resp = self.client.delete(f"/urology/patient/{self.patient.patient_id}/voice-caption/{caption_id}/delete/")
        self.assertEqual(del_resp.status_code, 200)
        self.assertTrue(del_resp.json()["success"])

    def test_urology_bulk_upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        # GET bulk upload
        get_resp = self.client.get("/urology/patients/bulk-upload/")
        self.assertEqual(get_resp.status_code, 200)
        self.assertContains(get_resp, "Bulk upload")

        # POST bulk upload
        mri_file = SimpleUploadedFile("patient_case_mri.nii.gz", b"NIFTI_CONTENT", content_type="application/gzip")
        wsi_file = SimpleUploadedFile("patient_case_wsi.tiff", b"TIFF_CONTENT", content_type="image/tiff")

        post_resp = self.client.post(
            "/urology/patients/bulk-upload/",
            data={
                "folder": self.folder.id,
                "files": [mri_file, wsi_file],
            },
            follow=True,
        )
        self.assertEqual(post_resp.status_code, 200)
        self.assertTrue(Patient.objects.filter(name__icontains="Patient Case Mri").exists())
        self.assertTrue(Patient.objects.filter(name__icontains="Patient Case Wsi").exists())

    def test_urology_raw_files_management(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        # Add raw file
        scan_file = SimpleUploadedFile("extra_mri.nii.gz", b"SCAN_DATA", content_type="application/gzip")
        add_resp = self.client.post(
            f"/urology/patient/{self.patient.patient_id}/files/raw/add/",
            data={"modality": "urology-mri", "file": scan_file},
        )
        self.assertEqual(add_resp.status_code, 200)
        add_data = add_resp.json()
        self.assertTrue(add_data["ok"])
        file_id = add_data["file"]["id"]

        # Delete raw file
        del_resp = self.client.post(f"/urology/patient/{self.patient.patient_id}/files/raw/{file_id}/delete/")
        self.assertEqual(del_resp.status_code, 200)
        self.assertTrue(del_resp.json()["ok"])

    def test_urology_profile_view(self):
        resp = self.client.get("/urology/profile/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "urology_admin")

