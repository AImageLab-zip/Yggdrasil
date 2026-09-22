"""Stored files are served so that an uploaded document cannot run as its viewer.

An annotator could store ``.html`` or ``.svg`` through a photo upload, and the
file was served inline, same-origin, under the type its extension claimed --
script running as whichever admin opened it.
"""

import io

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase

from common.file_access import (
    FILE_RESPONSE_CSP,
    content_disposition,
    parse_byte_range,
    served_content_type,
)
from common.models import FileRegistry, Project, ProjectAccess
from common.object_storage import get_object_storage


class ServedContentTypeTests(SimpleTestCase):
    def test_executable_types_become_octet_stream_attachments(self):
        for content_type in ("text/html", "image/svg+xml", "application/xml",
                             "text/javascript", "application/xhtml+xml", None, ""):
            with self.subTest(content_type=content_type):
                self.assertEqual(
                    served_content_type(content_type), ("application/octet-stream", True)
                )

    def test_display_types_stay_inline(self):
        for content_type in ("image/png", "image/jpeg", "video/mp4", "audio/webm",
                             "application/json", "IMAGE/PNG; charset=binary"):
            with self.subTest(content_type=content_type):
                served, attachment = served_content_type(content_type)
                self.assertFalse(attachment)
                self.assertEqual(served, content_type.split(";")[0].lower())

    def test_disposition_cannot_be_broken_out_of(self):
        header = content_disposition('evil"; filename=x.html\r\nX-Injected: 1', False)
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        ascii_part = header.split('filename="', 1)[1].split('"', 1)[0]
        self.assertNotIn('"', ascii_part)
        self.assertIn("filename*=UTF-8''", header)

    def test_non_ascii_names_survive_in_the_extended_parameter(self):
        header = content_disposition("radiografía.png", True)
        self.assertTrue(header.startswith("attachment;"))
        self.assertIn("radiograf%C3%ADa.png", header)


class ByteRangeTests(SimpleTestCase):
    def test_ranges(self):
        cases = {
            "bytes=0-99": (0, 99),
            "bytes=10-": (10, 999),
            "bytes=-100": (900, 999),
            "bytes=900-5000": (900, 999),
            "bytes=1000-": None,       # starts past the end
            "bytes=50-10": None,       # reversed
            "bytes=0-1,5-9": None,     # multi-range: serve whole file
            "bytes=-": None,
            "items=0-1": None,
            "": None,
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(parse_byte_range(header, 1000), expected)


class ServeStoredFileTests(TestCase):
    def setUp(self):
        from maxillo.models import Folder, Patient

        project = Project.objects.create(name="Safety", slug="file-safety", domain="maxillo")
        self.user = User.objects.create_user("viewer-safety")
        ProjectAccess.objects.create(user=self.user, project=project, role="viewer")
        self.patient = Patient.objects.create(
            name="p", project=project, folder=Folder.objects.create(name="f", project=project)
        )
        self.client.force_login(self.user)

    def _stored(self, key, body, **metadata):
        get_object_storage().upload_fileobj(io.BytesIO(body), key=key)
        return FileRegistry.objects.create(
            file_type="rgb_image", file_path=key, file_size=len(body), file_hash="0" * 64,
            domain="maxillo", patient=self.patient, metadata=metadata,
        )

    def test_an_uploaded_html_file_is_downloaded_not_rendered(self):
        f = self._stored("maxillo/raw/rgb/rgb_1.html", b"<script>alert(1)</script>",
                         original_filename="x.html")
        response = self.client.get(f"/api/processing/files/serve/{f.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(response["Content-Security-Policy"], FILE_RESPONSE_CSP)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_an_svg_is_not_served_as_an_image(self):
        f = self._stored("maxillo/raw/rgb/rgb_2.svg", b"<svg onload='alert(1)'/>")
        response = self.client.get(f"/api/processing/files/serve/{f.id}/")
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))

    def test_a_photo_is_still_shown_inline(self):
        f = self._stored("maxillo/raw/rgb/rgb_3.png", b"\x89PNG\r\n\x1a\n")
        response = self.client.get(f"/api/processing/files/serve/{f.id}/")
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertTrue(response["Content-Disposition"].startswith("inline;"))
        self.assertEqual(response["Content-Security-Policy"], FILE_RESPONSE_CSP)


class PhotoUploadExtensionTests(TestCase):
    def setUp(self):
        from maxillo.models import Patient

        project = Project.objects.create(name="Upload", slug="file-upload-safety", domain="maxillo")
        self.patient = Patient.objects.create(name="p", project=project)

    def test_non_image_extensions_are_refused(self):
        from maxillo.file_utils import save_intraoral_photos_to_dataset, save_rgb_images_to_dataset

        for name in ("x.html", "x.svg", "x.js", "noext"):
            with self.subTest(name=name, saver="rgb"):
                saved, errors = save_rgb_images_to_dataset(
                    self.patient, [SimpleUploadedFile(name, b"<svg/>")]
                )
                self.assertEqual(saved, [])
                self.assertEqual(len(errors), 1)
        for name in ("x.html", "x.svg"):
            with self.subTest(name=name, saver="intraoral"):
                saved, errors, *_ = save_intraoral_photos_to_dataset(
                    self.patient, [SimpleUploadedFile(name, b"<svg/>")]
                )
                self.assertEqual(saved, [])
                self.assertEqual(len(errors), 1)
        self.assertFalse(FileRegistry.objects.filter(patient=self.patient).exists())

    def test_photo_formats_seen_in_real_uploads_are_accepted(self):
        from maxillo.file_utils import save_rgb_images_to_dataset

        for name in ("a.jpg", "b.PNG", "c.heic", "d.tif", "e.bmp"):
            with self.subTest(name=name):
                saved, errors = save_rgb_images_to_dataset(
                    self.patient, [SimpleUploadedFile(name, b"img")]
                )
                self.assertEqual(errors, [])
                self.assertEqual(len(saved), 1)


class SiteCspTests(TestCase):
    def test_pages_carry_the_enforced_and_report_only_policies(self):
        response = self.client.get("/login/")
        self.assertIn("object-src 'none'", response["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", response["Content-Security-Policy"])
        self.assertIn("script-src", response["Content-Security-Policy-Report-Only"])


class AsgiDownloadStreamingTests(TestCase):
    """Under ASGI a download is streamed chunk by chunk, not collected first.

    Django's ASGI handler buffers a *sync* streaming body in full before sending
    it, which held every multi-GB download in one worker's memory.
    """

    async def test_downloads_are_async_streams_under_asgi(self):
        from asgiref.sync import sync_to_async

        user, file_id, body = await sync_to_async(self._fixture)()
        await self.async_client.aforce_login(user)
        response = await self.async_client.get(f"/api/processing/files/serve/{file_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_async)
        received = b"".join([chunk async for chunk in response.streaming_content])
        self.assertEqual(received, body)

    def _fixture(self):
        from maxillo.models import Patient

        project = Project.objects.create(name="Stream", slug="file-stream", domain="maxillo")
        user = User.objects.create_user("stream-viewer")
        ProjectAccess.objects.create(user=user, project=project, role="viewer")
        patient = Patient.objects.create(name="p", project=project)
        body = bytes(range(256)) * 20000  # several storage chunks
        key = "maxillo/raw/generic/stream.bin"
        get_object_storage().upload_fileobj(io.BytesIO(body), key=key)
        f = FileRegistry.objects.create(
            file_type="generic_raw", file_path=key, file_size=len(body), file_hash="0" * 64,
            domain="maxillo", patient=patient,
        )
        return user, f.id, body

    def test_wsgi_responses_stay_synchronous(self):
        user, file_id, body = self._fixture()
        self.client.force_login(user)
        response = self.client.get(f"/api/processing/files/serve/{file_id}/")
        self.assertFalse(response.is_async)
        self.assertEqual(b"".join(response), body)
