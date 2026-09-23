"""Cardiology: ECG validation, the rhythm call, uploads, the browser plot, export.

Cross-project authorization of every routed URL is covered generically by
``common.tests_cross_project_matrix``, and the shared routes by
``common.tests_domain_registry``; these tests are about what is cardiology's own.
"""

import io
import json
import uuid
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from annotations.queries import ecg_rhythm_values
from common.models import AnnotationMethod, FileRegistry, Project, ProjectAccess

from .exports import collect_ecg_classification, count_ecg_classifications
from .file_utils import InvalidEcgFile, validate_ecg_json
from .models import Folder, Patient
from .views import _next_patient_id


def _ecg(seconds=2.0, leads=2, frequency=500, **overrides):
    samples = int(seconds * frequency) + 1
    doc = {
        "frequency": frequency,
        "dataX": [i / frequency for i in range(samples)],
        "data": [{"title": f"L{n}", "values": [0.1] * samples} for n in range(leads)],
    }
    doc.update(overrides)
    return doc


def _upload(doc, name="rec.json"):
    raw = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    return SimpleUploadedFile(name, raw, content_type="application/json")


def _png(width=120, height=60, fmt="PNG"):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format=fmt)
    return SimpleUploadedFile("plot.png", buffer.getvalue(), content_type="image/png")


class EcgValidationTests(SimpleTestCase):
    def test_a_well_formed_recording_passes(self):
        self.assertEqual(validate_ecg_json(_upload(_ecg()))["frequency"], 500)

    def test_malformed_documents_are_refused_with_a_reason(self):
        cases = {
            "not json": b"{nope",
            "deep nesting": b"[" * 100000 + b"]" * 100000,
            "not an object": json.dumps([1, 2]).encode(),
            "missing field": json.dumps({"frequency": 1, "dataX": [0, 1]}).encode(),
            "bad frequency": json.dumps(_ecg(frequency=500) | {"frequency": "fast"}).encode(),
            "no leads": json.dumps(_ecg(data=[])).encode(),
            "string sample": json.dumps(_ecg(data=[{"title": "I", "values": ["x"]}])).encode(),
            "NaN sample": b'{"frequency": 1, "dataX": [0, 1], "data": [{"title": "I", "values": [NaN, 1]}]}',
            "time goes backwards": json.dumps(_ecg(dataX=[0, 2, 1])).encode(),
        }
        for label, raw in cases.items():
            with self.subTest(label), self.assertRaises(InvalidEcgFile):
                validate_ecg_json(_upload(raw))

    def test_a_recording_too_long_to_plot_is_refused_at_upload(self):
        # 12 leads at the plot's 75 px/s and 90 px/row: past ~197 s the canvas exceeds
        # the pixel budget the browser and save_browser_ecg_plot allow.
        with self.assertRaisesMessage(InvalidEcgFile, "too long to plot"):
            validate_ecg_json(_upload(_ecg(seconds=240, leads=12, frequency=10)))
        validate_ecg_json(_upload(_ecg(seconds=150, leads=12, frequency=10)))

    def test_an_oversized_file_is_refused_before_parsing(self):
        upload = _upload(_ecg())
        upload.size = 50 * 1024 * 1024
        with self.assertRaisesMessage(InvalidEcgFile, "larger than"):
            validate_ecg_json(upload)


class CardiologyTestCase(TestCase):
    def setUp(self):
        call_command("setup_cardiology_modalities", stdout=io.StringIO())
        self.project = Project.objects.get(slug="cardiology")
        self.folder = Folder.objects.create(name="Batch", project=self.project)
        self.admin = self._user("card-admin", "admin")
        self.annotator = self._user("card-annotator", "annotator")
        self.viewer = self._user("card-viewer", "viewer")
        self.patient = Patient.objects.create(name="P1", project=self.project, folder=self.folder)
        self.storage = mock.MagicMock()
        for target in ("common.uploads.get_object_storage", "cardiology.views.get_object_storage"):
            patcher = mock.patch(target, return_value=self.storage)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _user(self, username, role, project=None):
        user = User.objects.create_user(username=username, password="pw")
        ProjectAccess.objects.create(user=user, project=project or self.project, role=role)
        return user

    def _login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def _raw_ecg(self, patient=None):
        return FileRegistry.objects.create(
            file_type="ecg_raw", file_path="cardiology/raw/ecg/x.json", domain="cardiology",
            cardiology_patient=patient or self.patient, file_size=1, file_hash="x",
        )

    def _classify(self, value, revision=0, user=None, patient=None):
        self._login(user or self.annotator)
        return self.client.post(
            reverse("cardiology:classification_update", args=[(patient or self.patient).patient_id]),
            json.dumps({"value": value, "revision": revision}),
            content_type="application/json",
        )


class RhythmClassificationTests(CardiologyTestCase):
    def test_each_save_is_a_revision_and_a_stale_one_is_refused(self):
        first = self._classify("AF", revision=0)
        self.assertEqual(first.status_code, 200)
        self.assertEqual((first.json()["value"], first.json()["revision"]), ("AF", 1))

        second = self._classify("NSR", revision=1)
        self.assertEqual(second.json()["revision"], 2)

        stale = self._classify("Other", revision=1)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["value"], "NSR")
        self.assertEqual(ecg_rhythm_values(Patient.objects.all()), {self.patient.pk: "NSR"})

    def test_viewers_cannot_classify(self):
        self.assertEqual(self._classify("AF", user=self.viewer).status_code, 403)

    def test_a_value_outside_the_vocabulary_is_refused(self):
        self.assertEqual(self._classify("VT").status_code, 400)

    def test_a_project_without_the_method_refuses(self):
        self.project.annotation_methods.remove(AnnotationMethod.objects.get(slug="ecg_classification"))
        self.assertEqual(self._classify("AF").status_code, 403)

    def test_a_call_locks_the_raw_ecg(self):
        from common.annotation_lock import annotation_lock_reasons

        self._classify("AF")
        self.assertIn("an ECG rhythm classification", annotation_lock_reasons(self.patient))

    def test_the_export_document_and_preview_count(self):
        self._classify("AF")
        artifact = mock.Mock(filename=None)
        [(entry, size)] = list(collect_ecg_classification(self.patient, artifact))
        document = json.loads(entry["content"])
        self.assertEqual(document["manual"]["value"], {"code": "AF", "label": "AF"})
        self.assertEqual(document["manual"]["annotator"], "card-annotator")
        self.assertEqual(size, len(entry["content"].encode()))

        unclassified = Patient.objects.create(name="P2", project=self.project, folder=self.folder)
        self.assertEqual(list(collect_ecg_classification(unclassified, artifact)), [])
        self.assertEqual(count_ecg_classifications(Patient.objects.all())[0], 1)

    def test_list_filters_read_the_latest_call(self):
        other = Patient.objects.create(name="Unclassified", project=self.project, folder=self.folder)
        self._classify("AF")
        self._login(self.admin)
        url = reverse("cardiology:patient_list")

        classified = self.client.get(url, {"has_ecg_classification": "yes"})
        self.assertContains(classified, "P1")
        self.assertNotContains(classified, "Unclassified")

        empty = self.client.get(url, {"no_annotations": "yes"})
        self.assertContains(empty, other.name)
        self.assertNotContains(empty, f'data-patient-id="{self.patient.patient_id}"')


class PatientPageTests(CardiologyTestCase):
    def test_admins_page_renders_and_can_be_framed_by_the_warmup(self):
        self._raw_ecg()
        self._login(self.admin)
        response = self.client.get(reverse("cardiology:patient_detail", args=[self.patient.patient_id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("cardiology:rerun_processing", args=[self.patient.patient_id]))
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", response["Content-Security-Policy"])

    def test_next_follows_the_list_order_within_the_folder(self):
        newer = Patient.objects.create(name="Newer", project=self.project, folder=self.folder)
        elsewhere = Folder.objects.create(name="Other", project=self.project)
        Patient.objects.create(name="Elsewhere", project=self.project, folder=elsewhere)
        self.assertEqual(_next_patient_id(newer), self.patient.patient_id)
        self.assertIsNone(_next_patient_id(self.patient))


class UploadTests(CardiologyTestCase):
    def _post(self, user, **fields):
        self._login(user)
        data = {"name": "Uploaded", "project": self.project.id, "folder": self.folder.id}
        data.update(fields)
        return self.client.post(reverse("cardiology:upload_patient"), data)

    def test_an_annotator_uploads_a_recording(self):
        self._post(self.annotator, ecg=_upload(_ecg()))
        patient = Patient.objects.get(name="Uploaded")
        self.assertTrue(patient.files.filter(file_type="ecg_raw").exists())
        self.storage.upload_file.assert_called_once()

    def test_an_invalid_recording_creates_nothing(self):
        response = self._post(self.annotator, ecg=_upload(b"{nope"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Patient.objects.filter(name="Uploaded").exists())
        self.storage.upload_file.assert_not_called()

    def test_a_viewer_cannot_upload(self):
        self._post(self.viewer, ecg=_upload(_ecg()))
        self.assertFalse(Patient.objects.filter(name="Uploaded").exists())

    def test_bulk_upload_isolates_a_bad_file_and_is_admin_only(self):
        url = reverse("cardiology:bulk_upload_patients")
        self._login(self.annotator)
        self.assertEqual(
            self.client.post(url, {"folder": self.folder.id}, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code, 403
        )

        self._login(self.admin)
        response = self.client.post(
            url,
            {"folder": self.folder.id, "files": [_upload(_ecg(), "good.json"), _upload(b"{", "bad.json")]},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        body = response.json()
        self.assertEqual((body["created"], body["failed"]), (1, 1))
        self.assertTrue(Patient.objects.filter(name="good").exists())
        self.assertFalse(Patient.objects.filter(name="bad").exists())


class BrowserPlotTests(CardiologyTestCase):
    def _save(self, user=None, png=None, generation_uuid=None):
        self._login(user or self.annotator)
        return self.client.post(
            reverse("cardiology:save_browser_ecg_plot", args=[self.patient.patient_id]),
            {"plot_png": png or _png(), "generation_uuid": generation_uuid or str(uuid.uuid4())},
        )

    def test_the_first_plot_is_kept_and_later_ones_are_idempotent(self):
        self._raw_ecg()
        self.assertEqual(self._save().json()["outcome"], "created")
        self.assertEqual(self._save().json()["outcome"], "existing")
        self.assertEqual(self.patient.files.filter(file_type="ecg_processed").count(), 1)

    def test_bad_submissions_are_refused(self):
        self._raw_ecg()
        self.assertEqual(self._save(generation_uuid="../../etc").status_code, 400)
        self.assertEqual(self._save(png=_png(fmt="JPEG")).status_code, 400)
        self.assertEqual(self._save(png=_png(width=5000, height=4000)).status_code, 400)
        self.assertEqual(self._save(user=self.viewer).status_code, 403)
        self.assertFalse(self.patient.files.filter(file_type="ecg_processed").exists())

    def test_a_patient_without_a_recording_has_nothing_to_plot(self):
        self.assertEqual(self._save().status_code, 409)

    def test_a_failed_registration_removes_the_stored_object(self):
        self._raw_ecg()
        with mock.patch("cardiology.views.FileRegistry.objects.create", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                self._save()
        self.storage.delete.assert_called_once()
