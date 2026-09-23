from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, Client
from django.urls import reverse

import os
from io import BytesIO
from unittest.mock import MagicMock, patch

from common.models import FileRegistry, Modality, Project, ProjectAccess
from urology.models import Folder, Patient, VoiceCaption


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
        self.assertEqual(self.project.modalities.count(), 3)
        slugs = set(self.project.modalities.values_list("slug", flat=True))
        self.assertIn("urology-mri", slugs)
        self.assertIn("urology-wsi", slugs)
        self.assertIn("urology-confocal", slugs)
        mri_mod = self.project.modalities.get(slug="urology-mri")
        self.assertEqual(mri_mod.icon, "fas fa-magnet")

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
        self.assertContains(response, "Urology Confocale")
        # MRI icon in patient list matches patient view
        self.assertContains(response, "fa-magnet")

    def test_urology_filter_by_folder(self):
        response = self.client.get(f"/urology/patients/?folder={self.folder.id}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Patient 1")

    def test_urology_upload_get(self):
        upload_resp = self.client.get("/urology/upload/")
        self.assertEqual(upload_resp.status_code, 200)
        self.assertContains(upload_resp, "Upload patient data")
        self.assertContains(upload_resp, "<h3>MRI</h3>")
        self.assertContains(upload_resp, "<h3>WSI</h3>")
        self.assertContains(upload_resp, "<h3>Confocale</h3>")

    def test_urology_upload_post(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from common.models import FileRegistry, Job

        mri_file = SimpleUploadedFile("mri_scan.nii.gz", b"FAKE_NIFTI_DATA", content_type="application/gzip")
        wsi_file = SimpleUploadedFile("histology.tiff", b"FAKE_TIFF_DATA", content_type="image/tiff")
        confocal_file = SimpleUploadedFile("confocal_slice.tiff", b"FAKE_CONFOCAL_TIFF", content_type="image/tiff")

        post_data = {
            "name": "Uploaded Urology Patient",
            "project": self.project.id,
            "folder": self.folder.id,
            "tags_text": "upload-test, biopsy",
            "urology-mri": mri_file,
            "urology-wsi": wsi_file,
            "urology-confocal": confocal_file,
        }

        resp = self.client.post("/urology/upload/", data=post_data, follow=True)
        self.assertEqual(resp.status_code, 200)

        patient = Patient.objects.filter(name="Uploaded Urology Patient").first()
        self.assertIsNotNone(patient)
        self.assertEqual(patient.folder, self.folder)
        self.assertIn("biopsy", patient.tag_names())

        files = FileRegistry.objects.filter(domain="urology", urology_patient=patient)
        self.assertEqual(files.count(), 3)
        mri_fr = files.filter(modality__slug="urology-mri").first()
        wsi_fr = files.filter(modality__slug="urology-wsi").first()
        confocal_fr = files.filter(modality__slug="urology-confocal").first()
        self.assertIsNotNone(mri_fr)
        self.assertIsNotNone(wsi_fr)
        self.assertIsNotNone(confocal_fr)
        self.assertEqual(mri_fr.file_type, "urology_mri_raw")
        self.assertEqual(wsi_fr.file_type, "urology_wsi_raw")
        self.assertEqual(confocal_fr.file_type, "urology_confocal_raw")

        jobs = Job.objects.filter(domain="urology", urology_patient=patient)
        self.assertEqual(jobs.count(), 3)

    def test_urology_patient_detail(self):
        detail_resp = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(detail_resp.status_code, 200)
        self.assertContains(detail_resp, self.patient.name)
        self.assertContains(detail_resp, f"ID {self.patient.patient_id}")
        # The modality selector is the shared strip, built from patient_modalities,
        # not a hardcoded per-domain bar.
        self.assertContains(detail_resp, 'id="modalityToggleGroup"')
        for slug in ("urology-mri", "urology-wsi", "urology-confocal"):
            self.assertContains(detail_resp, f'data-modality="{slug}"')
        self.assertContains(detail_resp, "urologyMriStage")
        self.assertContains(detail_resp, "urologyWsiStagePanel")
        self.assertContains(detail_resp, "urologyConfocalStagePanel")
        self.assertNotContains(detail_resp, "Split Correlation View")
        self.assertNotContains(detail_resp, "MRI (mpMRI)")
        self.assertNotContains(detail_resp, "WSI (Histopathology)")

        # Verify Captions menu/tab is active by default instead of Files
        self.assertContains(detail_resp, 'class="side-tab is-active" data-tab-target="captions"')
        self.assertContains(detail_resp, 'class="side-tab-pane is-active" data-tab-pane="captions"')
        self.assertNotContains(detail_resp, 'class="side-tab is-active" data-tab-target="files"')
        self.assertNotContains(detail_resp, 'class="side-tab-pane is-active" data-tab-pane="files"')

    def test_urology_report_template_voices(self):
        detail_resp = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(detail_resp.status_code, 200)

        # Verify modality section headers in Italian
        self.assertContains(detail_resp, "Risonanza Magnetica Multiparametrica")
        self.assertContains(detail_resp, "Microscopia Confocale Real-Time")
        self.assertContains(detail_resp, "Istopatologia Digitale")

        # Verify 1. MRI report voices
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

        # Verify 2. Confocale report voices
        self.assertContains(detail_resp, "Adeguatezza del Prelievo e Rappresentatività")
        self.assertContains(detail_resp, "Architettura Ghiandolare ed Orientamento Acinare")
        self.assertContains(detail_resp, "Morfologia Nucleare ed Atipia Cellulare")
        self.assertContains(detail_resp, "Integrità dello Strato di Cellule Basali")
        self.assertContains(detail_resp, "Infiltrazione Stromale e Reazione Desmoplastica")
        self.assertContains(detail_resp, "Margini Chirurgici in Tempo Reale (NeuroSAFE)")

        # Verify 3. WSI report voices
        self.assertContains(detail_resp, "Istotipo Tumorale")
        self.assertContains(detail_resp, "Gleason Score e Grade Group ISUP")
        self.assertContains(detail_resp, "Percentuale Pattern 4 o 5 e Architettura Cribriforme")
        self.assertContains(detail_resp, "Carcinoma Intraduttale")
        self.assertContains(detail_resp, "Invasione Perineurale e Vascolare")
        self.assertContains(detail_resp, "Estensione Tumorale nel Prelievo e Margini Chirurgici")
        self.assertContains(detail_resp, "Parenchima Non Tumorale e Lesioni Concomitanti")

        # Verify multimodal section 4 is removed
        self.assertNotContains(detail_resp, "Correlazione Radio-Patologica e Reperti Conclusivi")

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
        # The shared export templates are rendered under urology's namespace, so
        # every link they build has to stay inside /urology/.
        self.assertContains(resp, "/urology/export/new/")
        self.assertNotContains(resp, "/maxillo/export/")

        # Verify fallback to active urology project when session is unset or cross-domain
        session = self.client.session
        if "current_project_id" in session:
            del session["current_project_id"]
        session.save()

        new_resp = self.client.get("/urology/export/new/")
        self.assertEqual(new_resp.status_code, 200)
        self.assertContains(new_resp, 'action="/urology/export/new/"')
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
        with patch("common.domain_views.export.artifact_exists", return_value=True):
            landing_resp = self.client.get(f"/urology/export/shared/{export.share_token}/")
            self.assertEqual(landing_resp.status_code, 200)
            # Shared landing is the shared template under urology's namespace.
        self.assertContains(landing_resp, "/urology/export/shared/")

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

    def test_urology_patient_detail_no_drag_into_window(self):
        resp = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Drag onto a window:")
        self.assertNotContains(resp, "modalityChipsContainer")
        self.assertNotContains(resp, "drop-hint")
        self.assertNotContains(resp, "Drop modality here")
        # Modality switcher buttons are clean without bracketed text
        self.assertContains(resp, 'data-modality="urology-mri"')
        self.assertContains(resp, 'data-modality="urology-wsi"')
        self.assertContains(resp, 'data-modality="urology-confocal"')
        self.assertNotContains(resp, "(mpMRI)")
        self.assertNotContains(resp, "(Histopathology)")
        # Top right available images notice removed
        self.assertNotContains(resp, 'id="urologyModeNotice"')
        # Appropriate empty state for missing MRI
        self.assertContains(resp, "No MRI Scan Uploaded")
        # Report template voices divided per modality
        self.assertContains(resp, 'data-urology-modality="mri"')
        self.assertContains(resp, 'data-urology-modality="confocal"')
        self.assertContains(resp, 'data-urology-modality="wsi"')

    def test_urology_annotations_mri_persistence(self):
        mri_mod = Modality.objects.get(slug="urology-mri")
        mri_file = FileRegistry.objects.create(
            urology_patient=self.patient,
            domain="urology",
            modality=mri_mod,
            file_type="urology_mri_raw",
            file_path=f"urology/{self.patient.patient_id}/test_mri.nii.gz",
            file_size=2048,
            file_hash="hash_mri_test",
        )

        mri_payload = {
            "fileId": mri_file.id,
            "expectedRevision": 0,
            "coordinateSystem": "patient_lps_mm",
            "volumeDescriptor": {"shape": [64, 64, 32]},
            "annotations": [
                {
                    "annotationUID": "temp-mri-len-1",
                    "metadata": {
                        "toolName": "Length",
                        "FrameOfReferenceUID": "1.2.840.10008.1",
                        "referencedImageId": "nifti:test_mri.nii.gz",
                    },
                    "data": {
                        "handles": {"points": [[0.0, 0.0, 0.0], [15.0, 0.0, 0.0]]},
                    },
                }
            ],
        }

        save_resp = self.client.post(
            f"/urology/api/patients/{self.patient.patient_id}/measurements/",
            data=mri_payload,
            content_type="application/json",
        )
        self.assertEqual(save_resp.status_code, 200)
        self.assertEqual(save_resp.json()["revision"], 1)

        # Check persistence via measurements state API
        state_resp = self.client.get(
            f"/urology/api/patients/{self.patient.patient_id}/measurements/state/?fileId={mri_file.id}"
        )
        self.assertEqual(state_resp.status_code, 200)
        state_data = state_resp.json()
        self.assertEqual(state_data["revision"], 1)
        self.assertEqual(len(state_data["annotations"]), 1)
        self.assertEqual(state_data["annotations"][0]["metadata"]["toolName"], "Length")

    def test_urology_annotations_wsi_persistence(self):
        wsi_mod = Modality.objects.get(slug="urology-wsi")
        wsi_file = FileRegistry.objects.create(
            urology_patient=self.patient,
            domain="urology",
            modality=wsi_mod,
            file_type="urology_wsi_raw",
            file_path=f"urology/{self.patient.patient_id}/test_wsi.tiff",
            file_size=4096,
            file_hash="hash_wsi_test",
        )

        wsi_payload = {
            "fileId": wsi_file.id,
            "expectedRevision": 0,
            "coordinateSystem": "image_pixel",
            "volumeDescriptor": {
                "shape": [2000, 2000],
                "mpp_x": 0.25,
                "mpp_y": 0.25,
                "tile_size": 256,
                "levels": 3,
                "recorded_by": "wsi-viewer",
            },
            "annotations": [
                {
                    "annotationUID": "temp-wsi-len",
                    "metadata": {"toolName": "Length"},
                    "data": {"handles": {"points": [[10.0, 10.0], [60.0, 60.0]]}},
                },
                {
                    "annotationUID": "temp-wsi-rect",
                    "metadata": {"toolName": "RectangleROI"},
                    "data": {"handles": {"points": [[100.0, 100.0], [200.0, 100.0], [100.0, 200.0], [200.0, 200.0]]}},
                },
                {
                    "annotationUID": "temp-wsi-circle",
                    "metadata": {"toolName": "CircleROI"},
                    "data": {"handles": {"points": [[300.0, 300.0], [350.0, 300.0]]}},
                },
                {
                    "annotationUID": "temp-wsi-spline",
                    "metadata": {"toolName": "SplineROI"},
                    "data": {"handles": {"points": [[400.0, 400.0], [450.0, 420.0], [430.0, 470.0]]}},
                },
                {
                    "annotationUID": "temp-wsi-label",
                    "metadata": {"toolName": "Label"},
                    "data": {"handles": {"points": [[500.0, 500.0]]}, "label": "WSI Biopsy Focus"},
                },
            ],
        }

        save_resp = self.client.post(
            f"/urology/api/patients/{self.patient.patient_id}/measurements/",
            data=wsi_payload,
            content_type="application/json",
        )
        self.assertEqual(save_resp.status_code, 200)
        self.assertEqual(save_resp.json()["revision"], 1)

        # Check persistence via patient detail reload
        detail_resp = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(detail_resp.status_code, 200)
        wsi_data = detail_resp.context["wsi_data"]
        self.assertIsNotNone(wsi_data)
        self.assertEqual(wsi_data["revision"], 1)
        self.assertEqual(len(wsi_data["annotations"]), 5)
        tools = [a["metadata"]["toolName"] for a in wsi_data["annotations"]]
        self.assertIn("Length", tools)
        self.assertIn("RectangleROI", tools)
        self.assertIn("CircleROI", tools)
        self.assertIn("SplineROI", tools)
        self.assertIn("Label", tools)

    def test_urology_annotations_confocal_persistence(self):
        confocal_mod = Modality.objects.get(slug="urology-confocal")
        confocal_file = FileRegistry.objects.create(
            urology_patient=self.patient,
            domain="urology",
            modality=confocal_mod,
            file_type="urology_confocal_raw",
            file_path=f"urology/{self.patient.patient_id}/test_confocal.tiff",
            file_size=4096,
            file_hash="hash_confocal_test",
        )

        confocal_payload = {
            "fileId": confocal_file.id,
            "expectedRevision": 0,
            "coordinateSystem": "image_pixel",
            "volumeDescriptor": {
                "shape": [1024, 1024],
                "mpp_x": 0.5,
                "mpp_y": 0.5,
                "tile_size": 256,
                "levels": 2,
                "recorded_by": "wsi-viewer",
            },
            "annotations": [
                {
                    "annotationUID": "temp-confocal-spline",
                    "metadata": {"toolName": "SplineROI"},
                    "data": {"handles": {"points": [[50.0, 50.0], [150.0, 60.0], [120.0, 140.0]]}},
                },
                {
                    "annotationUID": "temp-confocal-label",
                    "metadata": {"toolName": "Label"},
                    "data": {"handles": {"points": [[200.0, 200.0]]}, "label": "Atypical margin"},
                },
            ],
        }

        save_resp = self.client.post(
            f"/urology/api/patients/{self.patient.patient_id}/measurements/",
            data=confocal_payload,
            content_type="application/json",
        )
        self.assertEqual(save_resp.status_code, 200)
        self.assertEqual(save_resp.json()["revision"], 1)

        # Check persistence via patient detail reload
        detail_resp = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(detail_resp.status_code, 200)
        confocal_data = detail_resp.context["confocal_data"]
        self.assertIsNotNone(confocal_data)
        self.assertEqual(confocal_data["revision"], 1)
        self.assertEqual(len(confocal_data["annotations"]), 2)
        tools = [a["metadata"]["toolName"] for a in confocal_data["annotations"]]
        self.assertIn("SplineROI", tools)
        self.assertIn("Label", tools)

    def test_urology_annotations_multimodal_isolation_and_coexistence(self):
        mri_mod = Modality.objects.get(slug="urology-mri")
        wsi_mod = Modality.objects.get(slug="urology-wsi")
        confocal_mod = Modality.objects.get(slug="urology-confocal")

        mri_file = FileRegistry.objects.create(
            urology_patient=self.patient,
            domain="urology",
            modality=mri_mod,
            file_type="urology_mri_raw",
            file_path=f"urology/{self.patient.patient_id}/iso_mri.nii.gz",
            file_size=2048,
            file_hash="hash_iso_mri",
        )
        wsi_file = FileRegistry.objects.create(
            urology_patient=self.patient,
            domain="urology",
            modality=wsi_mod,
            file_type="urology_wsi_raw",
            file_path=f"urology/{self.patient.patient_id}/iso_wsi.tiff",
            file_size=4096,
            file_hash="hash_iso_wsi",
        )
        confocal_file = FileRegistry.objects.create(
            urology_patient=self.patient,
            domain="urology",
            modality=confocal_mod,
            file_type="urology_confocal_raw",
            file_path=f"urology/{self.patient.patient_id}/iso_confocal.tiff",
            file_size=4096,
            file_hash="hash_iso_confocal",
        )

        # Step 1: Save MRI annotation
        save_mri = self.client.post(
            f"/urology/api/patients/{self.patient.patient_id}/measurements/",
            data={
                "fileId": mri_file.id,
                "expectedRevision": 0,
                "coordinateSystem": "patient_lps_mm",
                "annotations": [
                    {
                        "annotationUID": "mri-ann-1",
                        "metadata": {"toolName": "Length"},
                        "data": {"handles": {"points": [[0.0, 0.0, 0.0], [20.0, 0.0, 0.0]]}},
                    }
                ],
            },
            content_type="application/json",
        )
        self.assertEqual(save_mri.status_code, 200)
        rev1 = save_mri.json()["revision"]

        # Detail check after Step 1: MRI has annotations, WSI and Confocal have 0
        resp1 = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(len(resp1.context["wsi_data"]["annotations"]), 0)
        self.assertEqual(len(resp1.context["confocal_data"]["annotations"]), 0)

        # Step 2: Save WSI annotation (replaces WSI group, carries forward MRI group)
        save_wsi = self.client.post(
            f"/urology/api/patients/{self.patient.patient_id}/measurements/",
            data={
                "fileId": wsi_file.id,
                "expectedRevision": rev1,
                "coordinateSystem": "image_pixel",
                "annotations": [
                    {
                        "annotationUID": "wsi-ann-1",
                        "metadata": {"toolName": "Length"},
                        "data": {"handles": {"points": [[10.0, 10.0], [100.0, 100.0]]}},
                    }
                ],
            },
            content_type="application/json",
        )
        self.assertEqual(save_wsi.status_code, 200)
        rev2 = save_wsi.json()["revision"]

        # Detail check after Step 2: WSI has 1, Confocal still has 0
        resp2 = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(len(resp2.context["wsi_data"]["annotations"]), 1)
        self.assertEqual(len(resp2.context["confocal_data"]["annotations"]), 0)
        # MRI state still has its annotation
        mri_state = self.client.get(
            f"/urology/api/patients/{self.patient.patient_id}/measurements/state/?fileId={mri_file.id}"
        ).json()
        self.assertEqual(len(mri_state["annotations"]), 1)

        # Step 3: Save Confocal annotation (carries forward MRI and WSI groups)
        save_confocal = self.client.post(
            f"/urology/api/patients/{self.patient.patient_id}/measurements/",
            data={
                "fileId": confocal_file.id,
                "expectedRevision": rev2,
                "coordinateSystem": "image_pixel",
                "annotations": [
                    {
                        "annotationUID": "confocal-ann-1",
                        "metadata": {"toolName": "Length"},
                        "data": {"handles": {"points": [[5.0, 5.0], [50.0, 50.0]]}},
                    }
                ],
            },
            content_type="application/json",
        )
        self.assertEqual(save_confocal.status_code, 200)
        rev3 = save_confocal.json()["revision"]

        # Detail check after Step 3: All 3 modalities coexist without leaking
        resp3 = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(len(resp3.context["wsi_data"]["annotations"]), 1)
        self.assertEqual(resp3.context["wsi_data"]["annotations"][0]["metadata"]["toolName"], "Length")
        self.assertEqual(
            resp3.context["wsi_data"]["annotations"][0]["data"]["handles"]["points"],
            [[10.0, 10.0], [100.0, 100.0]],
        )
        self.assertEqual(len(resp3.context["confocal_data"]["annotations"]), 1)
        self.assertEqual(resp3.context["confocal_data"]["annotations"][0]["metadata"]["toolName"], "Length")
        self.assertEqual(
            resp3.context["confocal_data"]["annotations"][0]["data"]["handles"]["points"],
            [[5.0, 5.0], [50.0, 50.0]],
        )

        mri_state3 = self.client.get(
            f"/urology/api/patients/{self.patient.patient_id}/measurements/state/?fileId={mri_file.id}"
        ).json()
        self.assertEqual(len(mri_state3["annotations"]), 1)
        self.assertEqual(mri_state3["annotations"][0]["metadata"]["toolName"], "Length")
        self.assertEqual(
            mri_state3["annotations"][0]["data"]["handles"]["points"],
            [[0.0, 0.0, 0.0], [20.0, 0.0, 0.0]],
        )


class UrologyTextCaptionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_superuser(
            username="uro_admin",
            email="uro_admin@example.com",
            password="pass",
        )
        self.annotator1 = User.objects.create_user(
            username="uro_annotator1",
            email="uro_annotator1@example.com",
            password="pass",
        )
        self.annotator2 = User.objects.create_user(
            username="uro_annotator2",
            email="uro_annotator2@example.com",
            password="pass",
        )
        self.viewer = User.objects.create_user(
            username="uro_viewer",
            email="uro_viewer@example.com",
            password="pass",
        )

        call_command("setup_urology_modalities")
        self.project = Project.objects.get(slug="urology")

        ProjectAccess.objects.create(user=self.admin, project=self.project, role="admin")
        ProjectAccess.objects.create(user=self.annotator1, project=self.project, role="annotator")
        ProjectAccess.objects.create(user=self.annotator2, project=self.project, role="annotator")
        ProjectAccess.objects.create(user=self.viewer, project=self.project, role="viewer")

        self.folder = Folder.objects.create(
            name="Caption Test Folder", project=self.project, created_by=self.admin
        )
        self.patient = Patient.objects.create(
            name="Caption Test Patient",
            folder=self.folder,
            project=self.project,
            uploaded_by=self.admin,
        )

    def test_text_caption_minimum_characters_validation(self):
        self.client.login(username="uro_annotator1", password="pass")
        url = reverse("urology:upload_text_caption", kwargs={"patient_id": self.patient.patient_id})

        # Under 10 characters -> blocked with HTTP 400
        resp_short = self.client.post(
            url,
            data={"text": "Short", "modality": "urology-mri"},
            content_type="application/json",
        )
        self.assertEqual(resp_short.status_code, 400)
        self.assertIn("at least 10 characters", resp_short.json()["error"])
        self.assertEqual(VoiceCaption.objects.count(), 0)

        # Empty or spaces only -> blocked with HTTP 400
        resp_empty = self.client.post(
            url,
            data={"text": "      ", "modality": "urology-mri"},
            content_type="application/json",
        )
        self.assertEqual(resp_empty.status_code, 400)
        self.assertEqual(VoiceCaption.objects.count(), 0)

        # Exactly 10 characters -> accepted with HTTP 200
        resp_valid = self.client.post(
            url,
            data={"text": "1234567890", "modality": "urology-mri"},
            content_type="application/json",
        )
        self.assertEqual(resp_valid.status_code, 200)
        self.assertEqual(VoiceCaption.objects.count(), 1)
        caption = VoiceCaption.objects.first()
        self.assertEqual(caption.text_caption, "1234567890")
        self.assertEqual(caption.original_text_caption, "1234567890")
        self.assertEqual(caption.modality, "urology-mri")
        self.assertEqual(caption.processing_status, "completed")

    def test_text_caption_modalities_assignment(self):
        self.client.login(username="uro_annotator1", password="pass")
        url = reverse("urology:upload_text_caption", kwargs={"patient_id": self.patient.patient_id})

        # WSI text caption
        resp_wsi = self.client.post(
            url,
            data={"text": "Histopathology Gleason 3+4 observed in left lobe", "modality": "urology-wsi"},
            content_type="application/json",
        )
        self.assertEqual(resp_wsi.status_code, 200)
        data_wsi = resp_wsi.json()["caption"]
        self.assertEqual(data_wsi["modality"], "urology-wsi")
        self.assertEqual(data_wsi["display_duration"], "Text")
        self.assertIsNone(data_wsi["audio_url"])

        # Confocale text caption
        resp_conf = self.client.post(
            url,
            data={"text": "Fluorescence confocal slice shows cellular density", "modality": "urology-confocal"},
            content_type="application/json",
        )
        self.assertEqual(resp_conf.status_code, 200)
        data_conf = resp_conf.json()["caption"]
        self.assertEqual(data_conf["modality"], "urology-confocal")

    def test_update_voice_caption_modality(self):
        self.client.login(username="uro_annotator1", password="pass")
        caption = VoiceCaption.objects.create(
            patient=self.patient,
            user=self.annotator1,
            modality="urology-mri",
            duration=0.0,
            text_caption="Valid test caption text",
            original_text_caption="Valid test caption text",
            processing_status="completed",
        )

        url = reverse(
            "urology:update_voice_caption_modality",
            kwargs={"patient_id": self.patient.patient_id, "caption_id": caption.id},
        )

        # Update to valid modality
        resp = self.client.post(
            url,
            data={"modality": "urology-confocal"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        caption.refresh_from_db()
        self.assertEqual(caption.modality, "urology-confocal")

        # Invalid modality
        resp_invalid = self.client.post(
            url,
            data={"modality": "invalid-modality-xyz"},
            content_type="application/json",
        )
        self.assertEqual(resp_invalid.status_code, 400)

    def test_edit_and_revert_voice_caption(self):
        self.client.login(username="uro_annotator1", password="pass")
        caption = VoiceCaption.objects.create(
            patient=self.patient,
            user=self.annotator1,
            modality="urology-wsi",
            duration=0.0,
            text_caption="Original pathology note",
            original_text_caption="Original pathology note",
            processing_status="completed",
        )

        url = reverse(
            "urology:edit_voice_caption_transcription",
            kwargs={"patient_id": self.patient.patient_id, "caption_id": caption.id},
        )

        # Edit caption
        edit_resp = self.client.post(
            url,
            data={"action": "edit", "text": "Corrected pathology note with details"},
            content_type="application/json",
        )
        self.assertEqual(edit_resp.status_code, 200)
        caption.refresh_from_db()
        self.assertTrue(caption.is_edited)
        self.assertEqual(caption.text_caption, "Corrected pathology note with details")
        self.assertEqual(caption.original_text_caption, "Original pathology note")

        # Revert caption
        rev_resp = self.client.post(
            url,
            data={"action": "revert"},
            content_type="application/json",
        )
        self.assertEqual(rev_resp.status_code, 200)
        caption.refresh_from_db()
        self.assertFalse(caption.is_edited)
        self.assertEqual(caption.text_caption, "Original pathology note")

    def test_caption_permissions_viewer_and_bias_protection(self):
        caption1 = VoiceCaption.objects.create(
            patient=self.patient,
            user=self.annotator1,
            modality="urology-mri",
            duration=0.0,
            text_caption="Annotator 1 clinical finding",
            original_text_caption="Annotator 1 clinical finding",
            processing_status="completed",
        )

        # 1. Owner annotator views detail: can view content, not ghost
        self.client.login(username="uro_annotator1", password="pass")
        resp1 = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(resp1.status_code, 200)
        c1 = next(c for c in resp1.context["voice_captions"] if c.id == caption1.id)
        self.assertTrue(c1.can_view_content)
        self.assertFalse(c1.is_ghost)
        self.assertContains(resp1, "Annotator 1 clinical finding")

        # 2. Peer annotator views detail: content hidden for bias protection
        self.client.login(username="uro_annotator2", password="pass")
        resp2 = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(resp2.status_code, 200)
        c2 = next(c for c in resp2.context["voice_captions"] if c.id == caption1.id)
        self.assertFalse(c2.can_view_content)
        self.assertTrue(c2.is_ghost)
        self.assertContains(resp2, "Caption content hidden")

        # 3. Viewer views detail: can see all captions
        self.client.login(username="uro_viewer", password="pass")
        resp3 = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(resp3.status_code, 200)
        c3 = next(c for c in resp3.context["voice_captions"] if c.id == caption1.id)
        self.assertTrue(c3.can_view_content)
        self.assertFalse(c3.is_ghost)
        self.assertContains(resp3, "Annotator 1 clinical finding")

        # 4. Admin views detail: can view and edit all
        self.client.login(username="uro_admin", password="pass")
        resp4 = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(resp4.status_code, 200)
        c4 = next(c for c in resp4.context["voice_captions"] if c.id == caption1.id)
        self.assertTrue(c4.can_view_content)
        self.assertTrue(c4.can_edit_content)
        self.assertFalse(c4.is_ghost)

    def test_delete_voice_caption_permissions(self):
        caption = VoiceCaption.objects.create(
            patient=self.patient,
            user=self.annotator1,
            modality="urology-mri",
            duration=0.0,
            text_caption="Caption to be deleted",
            original_text_caption="Caption to be deleted",
            processing_status="completed",
        )
        url = reverse(
            "urology:delete_voice_caption",
            kwargs={"patient_id": self.patient.patient_id, "caption_id": caption.id},
        )

        # Non-owner annotator cannot delete
        self.client.login(username="uro_annotator2", password="pass")
        resp_denied = self.client.delete(url)
        self.assertEqual(resp_denied.status_code, 403)
        self.assertEqual(resp_denied.json()["code"], "not_owner")

        # Admin deleting someone else's caption requires admin confirmation
        self.client.login(username="uro_admin", password="pass")
        resp_conf_req = self.client.delete(url)
        self.assertEqual(resp_conf_req.status_code, 403)
        self.assertEqual(resp_conf_req.json()["code"], "admin_confirmation_required")

        # Admin with confirmation succeeds
        resp_admin_del = self.client.delete(
            url,
            data={"admin_confirmed": True},
            content_type="application/json",
        )
        self.assertEqual(resp_admin_del.status_code, 200)
        self.assertFalse(VoiceCaption.objects.filter(id=caption.id).exists())

    def test_allowed_modalities_labels_in_patient_detail(self):
        self.client.login(username="uro_admin", password="pass")
        resp = self.client.get(f"/urology/patient/{self.patient.patient_id}/")
        self.assertEqual(resp.status_code, 200)

        modalities = resp.context["allowed_modalities"]
        names = [m["name"] for m in modalities]
        self.assertIn("MRI", names)
        self.assertIn("WSI", names)
        self.assertIn("Confocale", names)
        # Verify no awkward prefixes like 'Urology MRI'
        for name in names:
            self.assertFalse(name.startswith("Urology "))


class UrologyAuditRemediationTests(TestCase):
    def setUp(self):
        self.client = Client()
        call_command("setup_urology_modalities")
        self.project1 = Project.objects.get(slug="urology")
        self.project2 = Project.objects.create(
            name="Urology Project Two",
            slug="urology-two",
            domain="urology",
            is_active=True,
        )

        self.admin_user = User.objects.create_superuser(
            username="remediation_admin",
            email="rem_admin@example.com",
            password="pass",
        )
        self.proj1_admin = User.objects.create_user(
            username="proj1_admin",
            email="p1_admin@example.com",
            password="pass",
        )
        self.proj2_admin = User.objects.create_user(
            username="proj2_admin",
            email="p2_admin@example.com",
            password="pass",
        )
        self.plain_user = User.objects.create_user(
            username="plain_user",
            email="plain@example.com",
            password="pass",
        )

        ProjectAccess.objects.create(user=self.proj1_admin, project=self.project1, role="admin")
        ProjectAccess.objects.create(user=self.proj2_admin, project=self.project2, role="admin")
        ProjectAccess.objects.create(user=self.plain_user, project=self.project1, role="annotator")

        self.folder1 = Folder.objects.create(
            name="Folder 1", project=self.project1, created_by=self.admin_user
        )
        self.folder2 = Folder.objects.create(
            name="Folder 2", project=self.project2, created_by=self.admin_user
        )

        self.patient1 = Patient.objects.create(
            name="Patient 1",
            folder=self.folder1,
            project=self.project1,
            uploaded_by=self.admin_user,
        )
        self.patient2 = Patient.objects.create(
            name="Patient 2",
            folder=self.folder2,
            project=self.project2,
            uploaded_by=self.admin_user,
        )

    def test_rerun_picker_offers_only_the_patients_project_modalities(self):
        """The rerun picker never offers a modality the patient's project omits.

        ``project2`` registers no modality, so its patient's picker is empty even
        with the session pointed at ``project1`` (which registers all three) and
        even when the patient carries a file -- the patient's own project decides,
        as it does in maxillo and brain.
        """
        from django.core.files.uploadedfile import SimpleUploadedFile
        from urology.file_utils import save_urology_modality_file

        save_urology_modality_file(
            self.patient1,
            "urology-mri",
            SimpleUploadedFile("a.nii.gz", b"DATA", content_type="application/gzip"),
        )
        save_urology_modality_file(
            self.patient2,
            "urology-mri",
            SimpleUploadedFile("b.nii.gz", b"DATA", content_type="application/gzip"),
        )

        self.client.login(username="remediation_admin", password="pass")
        session = self.client.session
        session["current_project_id"] = self.project1.id
        session.save()

        resp = self.client.get(f"/urology/patient/{self.patient2.patient_id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["allowed_modality_slugs"], [])
        self.assertEqual(resp.context["rerunnable_step_slugs"], [])

        # project1 registers all three, but only the uploaded one is rerunnable.
        resp1 = self.client.get(f"/urology/patient/{self.patient1.patient_id}/")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(
            sorted(resp1.context["allowed_modality_slugs"]),
            ["urology-confocal", "urology-mri", "urology-wsi"],
        )
        self.assertNotIn("urology-wsi", resp1.context["rerunnable_step_slugs"])
        self.assertNotIn("urology-confocal", resp1.context["rerunnable_step_slugs"])

    def test_patient_list_rerun_picker_scoped_to_the_projects_modalities(self):
        """A project registering no modality offers none, rather than the domain's."""
        self.client.login(username="remediation_admin", password="pass")
        session = self.client.session
        session["current_project_id"] = self.project2.id
        session.save()

        resp = self.client.get("/urology/patients/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(resp.context["allowed_modalities"]), [])
        listed = list(resp.context["page_obj"])
        self.assertEqual(
            [item["patient"].patient_id for item in listed], [self.patient2.patient_id]
        )
        for item in listed:
            self.assertEqual(item["rerunnable_steps"], [])

    def test_save_urology_modality_file_unique_keys(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from urology.file_utils import save_urology_modality_file

        f1 = SimpleUploadedFile("scan.nii.gz", b"DATA1", content_type="application/gzip")
        f2 = SimpleUploadedFile("scan.nii.gz", b"DATA2", content_type="application/gzip")

        reg1, _ = save_urology_modality_file(self.patient1, "urology-mri", f1)
        reg2, _ = save_urology_modality_file(self.patient1, "urology-mri", f2)

        self.assertNotEqual(reg1.file_path, reg2.file_path)
        self.assertIn("urology-mri_patient_", reg1.file_path)
        self.assertIn("urology-mri_patient_", reg2.file_path)

    def test_raw_data_locking_add_raw_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.login(username="remediation_admin", password="pass")

        VoiceCaption.objects.create(
            patient=self.patient1,
            user=self.admin_user,
            text_caption="Locked by caption",
            processing_status="completed",
            duration=0.0,
        )

        test_file = SimpleUploadedFile("new_scan.nii.gz", b"DATA", content_type="application/gzip")
        url = reverse("urology:add_raw_file", kwargs={"patient_id": self.patient1.patient_id})
        resp = self.client.post(url, data={"modality": "urology-mri", "file": test_file})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("voice captions", resp.json()["error"].lower())

    def test_raw_data_locking_delete_raw_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from urology.file_utils import save_urology_modality_file

        f = SimpleUploadedFile("scan.nii.gz", b"DATA", content_type="application/gzip")
        reg, _ = save_urology_modality_file(self.patient1, "urology-mri", f)

        VoiceCaption.objects.create(
            patient=self.patient1,
            user=self.admin_user,
            text_caption="Locked by caption",
            processing_status="completed",
            duration=0.0,
        )

        self.client.login(username="remediation_admin", password="pass")
        url = reverse(
            "urology:delete_raw_file",
            kwargs={"patient_id": self.patient1.patient_id, "file_id": reg.id},
        )
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("voice captions", resp.json()["error"].lower())

    def test_raw_data_locking_bulk_purge(self):
        VoiceCaption.objects.create(
            patient=self.patient1,
            user=self.admin_user,
            text_caption="Locked by caption",
            processing_status="completed",
            duration=0.0,
        )

        self.client.login(username="remediation_admin", password="pass")
        url = reverse("urology:bulk_purge_patients")
        resp = self.client.post(
            url,
            data={"scan_ids": [self.patient1.patient_id]},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("is locked by existing annotations", resp.json()["error"])

    def test_project_aware_permissions_folders(self):
        # proj1_admin should not be able to rename folder2 in project2
        self.client.login(username="proj1_admin", password="pass")
        rename_url = reverse("urology:rename_folder", kwargs={"folder_id": self.folder2.id})
        resp = self.client.post(
            rename_url,
            data={"name": "Hacked Name"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

        # proj1_admin cannot move patient2 (project2) into folder1
        move_url = reverse("urology:move_patients_to_folder")
        resp = self.client.post(
            move_url,
            data={"scan_ids": [self.patient2.patient_id], "folder_id": self.folder1.id},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

        # proj1_admin cannot bulk delete patient2
        del_url = reverse("urology:bulk_delete_patients")
        resp = self.client.post(
            del_url,
            data={"scan_ids": [self.patient2.patient_id]},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

        # proj1_admin CAN bulk delete patient1
        resp_ok = self.client.post(
            del_url,
            data={"scan_ids": [self.patient1.patient_id]},
            content_type="application/json",
        )
        self.assertEqual(resp_ok.status_code, 200)
        self.patient1.refresh_from_db()
        self.assertTrue(self.patient1.deleted)

    def test_upload_patient_respects_form_cleaned_project(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.login(username="remediation_admin", password="pass")

        # Set session to project1
        session = self.client.session
        session["current_project_id"] = self.project1.id
        session.save()

        mri_file = SimpleUploadedFile("mri.nii.gz", b"NIFTI_BYTES", content_type="application/gzip")
        post_data = {
            "name": "Cross Project Patient",
            "project": self.project2.id,  # Explicitly select project2
            "folder": self.folder2.id,
            "urology-mri": mri_file,
        }

        resp = self.client.post("/urology/upload/", data=post_data, follow=True)
        self.assertEqual(resp.status_code, 200)

        created_patient = Patient.objects.get(name="Cross Project Patient")
        self.assertEqual(created_patient.project, self.project2)
        self.assertEqual(created_patient.folder, self.folder2)

    def test_create_folder_project_aware(self):
        # proj2_admin creates folder in project2 explicitly
        self.client.login(username="proj2_admin", password="pass")
        create_url = reverse("urology:create_folder")
        resp = self.client.post(
            create_url,
            data={"name": "P2 Folder", "project_id": self.project2.id},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        folder = Folder.objects.get(name="P2 Folder")
        self.assertEqual(folder.project, self.project2)

        # proj1_admin cannot create folder in project2
        self.client.login(username="proj1_admin", password="pass")
        resp = self.client.post(
            create_url,
            data={"name": "Hacked Folder", "project_id": self.project2.id},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

        # proj1_admin without explicit project_id creates in entry project (project1)
        resp = self.client.post(
            create_url,
            data={"name": "P1 Folder"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        p1_folder = Folder.objects.get(name="P1 Folder")
        self.assertEqual(p1_folder.project, self.project1)

    def test_add_and_remove_patients_from_folder_project_aware(self):
        self.client.login(username="proj1_admin", password="pass")

        # Cannot add patient2 (project2) to folder1
        add_url = reverse("urology:add_patients_to_folder")
        resp = self.client.post(
            add_url,
            data={"scan_ids": [self.patient2.patient_id], "folder_id": self.folder1.id},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

        # Cannot remove patient2 (project2) from folder2
        rem_url = reverse("urology:remove_patients_from_folder")
        resp = self.client.post(
            rem_url,
            data={"scan_ids": [self.patient2.patient_id], "folder_id": self.folder2.id},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_bulk_purge_patients_project_aware(self):
        self.client.login(username="proj1_admin", password="pass")
        purge_url = reverse("urology:bulk_purge_patients")

        # proj1_admin cannot purge patient2 in project2
        resp = self.client.post(
            purge_url,
            data={"scan_ids": [self.patient2.patient_id]},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

        # proj1_admin can purge patient1 in project1
        resp = self.client.post(
            purge_url,
            data={"scan_ids": [self.patient1.patient_id]},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Patient.objects.filter(patient_id=self.patient1.patient_id).exists())

    def test_patient_list_can_delete_is_project_aware(self):
        self.client.login(username="proj1_admin", password="pass")
        resp = self.client.get(reverse("urology:patient_list"))
        self.assertEqual(resp.status_code, 200)
        page_obj = resp.context["page_obj"]
        can_delete_map = {
            item["patient"].patient_id: item["can_delete"]
            for item in page_obj.object_list
        }
        self.assertTrue(can_delete_map.get(self.patient1.patient_id))
        # patient2 is in project2: proj1_admin must NOT have can_delete for it
        if self.patient2.patient_id in can_delete_map:
            self.assertFalse(can_delete_map[self.patient2.patient_id])

    def test_wsi_reader_decompression_bomb_protection(self):
        import warnings
        from PIL import Image
        import urology.wsi_reader  # noqa: F401

        self.assertEqual(Image.MAX_IMAGE_PIXELS, 250_000_000)
        # Verify Pillow issues DecompressionBombWarning when exceeding ceiling
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            Image._decompression_bomb_check((20_000, 20_000))  # 400M pixels > 250M ceiling
            self.assertTrue(
                any(issubclass(w.category, Image.DecompressionBombWarning) for w in caught_warnings)
            )

        # Verify Pillow raises DecompressionBombError beyond 2x ceiling
        with self.assertRaises(Image.DecompressionBombError):
            Image._decompression_bomb_check((30_000, 30_000))  # 900M pixels > 2 * 250M

    def test_save_urology_segmentation_file_and_api(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        from urology.file_utils import save_urology_modality_file, save_urology_segmentation_file

        self.client.login(username="proj1_admin", password="pass")

        # 1. Create a dummy TIFF slide file
        buf = BytesIO()
        im = Image.new("RGB", (100, 100), color="blue")
        im.save(buf, format="TIFF")
        buf.seek(0)
        slide_file = SimpleUploadedFile("biopsy.tiff", buf.getvalue(), content_type="image/tiff")
        slide_reg, _ = save_urology_modality_file(self.patient1, "urology-wsi", slide_file)

        # 2. Test segmentation file saving
        geo_content = b'{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Polygon","coordinates":[[[0,0],[10,0],[10,10],[0,10],[0,0]]]},"properties":{"classification":{"name":"Tumor","color":[200,0,0]}}}]}'
        seg_file = SimpleUploadedFile("biopsy.geojson", geo_content, content_type="application/geo+json")
        seg_reg, _ = save_urology_segmentation_file(self.patient1, "urology-wsi", seg_file, parent_file=slide_reg)

        self.assertEqual(seg_reg.file_type, "urology_segmentation")
        self.assertEqual(seg_reg.metadata.get("file_format"), "geojson")
        self.assertEqual(seg_reg.metadata.get("associated_image_file_id"), slide_reg.id)
        self.assertEqual(seg_reg.metadata.get("feature_count"), 1)

        # 3. Test wsi_segmentation_api
        url = reverse("urology:wsi_segmentation", kwargs={"file_id": slide_reg.id})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["hasSegmentation"])
        self.assertEqual(data["featureCount"], 1)
        self.assertEqual(data["classes"][0]["name"], "Tumor")
        self.assertEqual(data["geojson"]["type"], "FeatureCollection")

    def test_wsi_segmentation_api_none_when_no_segmentation(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        from urology.file_utils import save_urology_modality_file

        self.client.login(username="proj1_admin", password="pass")

        buf = BytesIO()
        im = Image.new("RGB", (50, 50), color="green")
        im.save(buf, format="TIFF")
        buf.seek(0)
        slide_file = SimpleUploadedFile("noseg.tiff", buf.getvalue(), content_type="image/tiff")
        slide_reg, _ = save_urology_modality_file(self.patient1, "urology-confocal", slide_file)

        url = reverse("urology:wsi_segmentation", kwargs={"file_id": slide_reg.id})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["hasSegmentation"])
        self.assertEqual(data["featureCount"], 0)
        self.assertIsNone(data["geojson"])





