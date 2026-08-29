"""Serving a stored series: the response shapes, and who may ask for them.

The shapes are not a matter of taste -- each is what
``@cornerstonejs/dicom-image-loader@5.8.2`` reads, and getting one subtly wrong renders
noise rather than raising. The access rules are findings F9 and F10, which is why they
are asserted here rather than assumed from a decorator.
"""

import io
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from pydicom import dcmread

from common.dicom.dicomweb import content_type_for, frame_bytes, instance_metadata
from common.dicom.ingest import ingest_dicom_series
from common.models import Project, ProjectAccess
from common.tests_dicom import synthetic_instance
from common.tests_dicom_ingest import FakeStorage, series_of
from maxillo.models import Folder, Patient


class ReadingStorage:
    """A FakeStorage that can also be read back, the way the views read it."""

    def __init__(self, objects):
        self.objects = objects

    def get(self, key):
        if key not in self.objects:
            raise FileNotFoundError(key)
        return io.BytesIO(self.objects[key]), mock.Mock()


@override_settings(SECURE_SSL_REDIRECT=False, DICOM_UID_HMAC_KEY="serving-key")
class DicomWebEndpointTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="serve", slug="serve", domain="maxillo")
        self.folder = Folder.objects.create(name="F", project=self.project)
        self.patient = Patient.objects.create(project=self.project, folder=self.folder)

        self.written = FakeStorage()
        with mock.patch(
            "common.dicom.ingest.get_object_storage", return_value=self.written
        ):
            [self.series] = ingest_dicom_series(
                self.patient, modality_slug="cbct", file_type="cbct_raw",
                files=series_of(3),
            )

        self.storage = ReadingStorage(self.written.objects)
        patcher = mock.patch(
            "common.dicom.views.get_object_storage", return_value=self.storage
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.user = User.objects.create_user(username="dcm-reader", password="x")  # noqa: S106
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)

    def metadata_url(self, study=None, series=None):
        return reverse(
            "api:api_dicomweb_series_metadata",
            kwargs={
                "study_uid": study or self.series.study_instance_uid,
                "series_uid": series or self.series.series_instance_uid,
            },
        )

    def frame_url(self, sop_uid=None, frames="1"):
        return reverse(
            "api:api_dicomweb_instance_frames",
            kwargs={
                "study_uid": self.series.study_instance_uid,
                "series_uid": self.series.series_instance_uid,
                "sop_uid": sop_uid or self.series.instances.first().sop_instance_uid,
                "frame_numbers": frames,
            },
        )

    # --- metadata --------------------------------------------------------------------

    def test_metadata_is_the_dicom_json_model_keyed_by_tag(self):
        response = self.client.get(self.metadata_url())
        self.assertEqual(response.status_code, 200)
        documents = response.json()
        self.assertEqual(len(documents), 3)

        first = documents[0]
        # Every tag below is one metaDataProvider.js reads by name. A rename in the
        # keep-list that dropped one would render a geometrically wrong volume.
        for tag in ("00280010", "00280011", "00280030", "00200032", "00200037", "00080060"):
            self.assertIn(tag, first, f"tag {tag} missing from the metadata response")
        self.assertEqual(first["00280010"]["Value"], [4])  # Rows

    def test_metadata_never_carries_the_pixels(self):
        # 7FE00010 base64'd into a 400-instance listing would be the whole study in
        # one response, and the frames endpoint exists precisely so it is not.
        documents = self.client.get(self.metadata_url()).json()
        for document in documents:
            self.assertNotIn("7FE00010", document)

    def test_metadata_carries_no_phi(self):
        documents = self.client.get(self.metadata_url()).json()
        rendered = str(documents)
        self.assertNotIn("SENTINEL", rendered)

    # --- frames ----------------------------------------------------------------------

    def test_a_frame_returns_the_pixel_bytes_not_the_file(self):
        response = self.client.get(self.frame_url())
        self.assertEqual(response.status_code, 200)
        # 4x4 16-bit = 32 bytes. A response carrying the whole instance would be far
        # larger, and extractMultipart would hand the header to the decoder as pixels.
        self.assertEqual(len(response.content), 32)

    def test_the_content_type_declares_the_transfer_syntax(self):
        """Without this parameter the loader assumes Implicit VR Little Endian.

        `loadImage.getTransferSyntaxForContentType` reads it off the response header.
        For an uncompressed frame the default happens to be right; for a JPEG Lossless
        CBCT it is silently wrong, and the image renders as noise with no error.
        """
        response = self.client.get(self.frame_url())
        self.assertIn("transfer-syntax=", response["Content-Type"])
        self.assertIn("1.2.840.10008.1.2.1", response["Content-Type"])

    def test_frames_are_one_based(self):
        self.assertEqual(self.client.get(self.frame_url(frames="0")).status_code, 404)

    def test_a_frame_past_the_end_is_a_404_not_an_empty_body(self):
        self.assertEqual(self.client.get(self.frame_url(frames="2")).status_code, 404)

    def test_more_than_one_frame_is_refused_rather_than_half_answered(self):
        # DICOMweb allows a list; the loader only ever asks for one, and answering
        # with the first would be a body that does not match what was requested.
        self.assertEqual(self.client.get(self.frame_url(frames="1,2")).status_code, 404)

    def test_an_unknown_instance_is_a_404(self):
        self.assertEqual(self.client.get(self.frame_url(sop_uid="9.9.9")).status_code, 404)

    def test_an_unknown_series_is_a_404(self):
        self.assertEqual(self.client.get(self.metadata_url(series="9.9.9")).status_code, 404)

    def test_a_series_uid_under_the_wrong_study_is_a_404(self):
        self.assertEqual(self.client.get(self.metadata_url(study="9.9.9")).status_code, 404)

    # --- access ----------------------------------------------------------------------

    def test_anonymous_callers_are_redirected_to_login(self):
        self.client.logout()
        for url in (self.metadata_url(), self.frame_url()):
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_a_user_without_project_access_gets_a_404(self):
        """F9: this namespace skips ActiveProfileMiddleware, so the view is the gate.

        404 rather than 403 deliberately -- whether a given SeriesInstanceUID exists is
        itself information, and a UID is a durable identifier.
        """
        outsider = User.objects.create_user(username="outsider", password="x")  # noqa: S106
        self.client.force_login(outsider)
        for url in (self.metadata_url(), self.frame_url()):
            self.assertEqual(self.client.get(url).status_code, 404)

    @override_settings(DEMO_GUEST_USERNAME="guest")
    def test_the_demo_guest_is_refused_by_default(self):
        """F10: demo_index logs anonymous visitors in as a real user.

        So @login_required alone would make stored DICOM anonymously fetchable for any
        is_demo folder the moment this route existed.
        """
        # The demo guest is a real, pre-existing account (common.demo), not one this
        # test invents -- which is the whole shape of F10.
        guest, _ = User.objects.get_or_create(username="guest")
        ProjectAccess.objects.update_or_create(
            user=guest, project=self.project, defaults={"role": "standard"}
        )
        self.client.force_login(guest)
        self.assertEqual(self.client.get(self.metadata_url()).status_code, 404)

    @override_settings(DEMO_GUEST_USERNAME="guest", DICOM_DEMO_ENABLED=True)
    def test_the_demo_guest_is_allowed_only_when_explicitly_enabled(self):
        guest, _ = User.objects.get_or_create(username="guest")
        ProjectAccess.objects.update_or_create(
            user=guest, project=self.project, defaults={"role": "standard"}
        )
        # The guest reads only curated folders, and that narrowing stays in force:
        # DICOM_DEMO_ENABLED lifts the DICOM-specific refusal, not the demo scoping.
        self.folder.is_demo = True
        self.folder.save(update_fields=["is_demo"])
        self.client.force_login(guest)
        self.assertEqual(self.client.get(self.metadata_url()).status_code, 200)

    @override_settings(DEMO_GUEST_USERNAME="guest", DICOM_DEMO_ENABLED=True)
    def test_enabling_the_demo_does_not_widen_folder_scoping(self):
        guest, _ = User.objects.get_or_create(username="guest")
        ProjectAccess.objects.update_or_create(
            user=guest, project=self.project, defaults={"role": "standard"}
        )
        self.client.force_login(guest)
        # Folder is not is_demo, so the ordinary permission layer still refuses.
        self.assertEqual(self.client.get(self.metadata_url()).status_code, 404)

    def test_only_get_is_allowed(self):
        self.assertEqual(self.client.post(self.metadata_url()).status_code, 405)


class FrameExtractionTests(TestCase):
    """The arithmetic, away from HTTP."""

    def test_a_multiframe_instance_slices_into_its_frames(self):
        dataset = synthetic_instance(rows=2, columns=2)
        dataset.NumberOfFrames = 3
        dataset.PixelData = bytes(range(2 * 2 * 2 * 3))

        first = frame_bytes(dataset, 1)
        third = frame_bytes(dataset, 3)
        self.assertEqual(len(first), 8)
        self.assertEqual(first, bytes(range(0, 8)))
        self.assertEqual(third, bytes(range(16, 24)))

    def test_metadata_round_trips_through_pydicoms_own_serialiser(self):
        dataset = synthetic_instance()
        document = instance_metadata(dataset)
        self.assertEqual(document["00080060"]["Value"], ["CT"])
        self.assertNotIn("7FE00010", document)

    def test_an_absent_transfer_syntax_falls_back_to_the_dicom_default(self):
        self.assertIn("1.2.840.10008.1.2", content_type_for(""))

    def test_a_stored_instance_reads_back_as_written(self):
        dataset = synthetic_instance()
        buffer = io.BytesIO()
        dataset.save_as(buffer, enforce_file_format=True)
        self.assertEqual(dcmread(io.BytesIO(buffer.getvalue())).Rows, 4)
