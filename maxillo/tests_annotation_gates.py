"""The two annotation gates F11 found missing: the affine rewrite and the
instant classification update.

``update_nifti_metadata`` rewrites a raw CBCT's qform/sform in place and
restamps ``FileRegistry.file_hash``. Every landmark, spline and polygon drawn on
that volume keeps its stored coordinates while the volume moves underneath them,
so it must refuse once annotation work exists -- the same rule
``maxillo.views.file_management`` already applies to adding or removing a raw
file. ``update_classification`` was the one annotation write that never asked
``project_allows_annotation``.
"""
import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from common.models import (
    AnnotationMethod,
    FileRegistry,
    Modality,
    Project,
    ProjectAccess,
)
from maxillo.models import Classification, Folder, Patient, VoiceCaption


@override_settings(SECURE_SSL_REDIRECT=False)
class AnnotationGateTestCase(TestCase):
    """A maxillo project admin on a patient in a folder, with a CBCT modality."""

    def setUp(self):
        self.project, _ = Project.objects.update_or_create(
            slug="maxillo", defaults={"name": "maxillo", "domain": "maxillo"}
        )
        self.user = User.objects.create_user(username="gate-admin", password="x")
        ProjectAccess.objects.create(
            user=self.user, project=self.project, role="admin"
        )
        self.client.force_login(self.user)
        self.folder = Folder.objects.create(name="Gates", project=self.project)
        self.patient = Patient.objects.create(
            name="Gated patient", folder=self.folder, project=self.project
        )
        self.modality = Modality.objects.create(name="Gate CBCT", slug="cbct")
        self.patient.modalities.add(self.modality)

    def _annotate(self):
        """The cheapest lock trigger: a voice caption exists on all domains."""
        return VoiceCaption.objects.create(
            patient=self.patient, user=self.user, duration=1.0
        )


class AffineRewriteRefusesWhenAnnotatedTests(AnnotationGateTestCase):
    def setUp(self):
        super().setUp()
        self.raw = FileRegistry.objects.create(
            file_type="cbct_raw",
            file_path="maxillo/raw/gate.nii.gz",
            file_size=1,
            file_hash="0" * 64,
            patient=self.patient,
            modality=self.modality,
            domain="maxillo",
        )
        self.url = reverse(
            "maxillo:update_nifti_metadata",
            kwargs={"patient_id": self.patient.patient_id},
        )
        self.affine = [
            [1.0, 0.0, 0.0, 10.0],
            [0.0, 1.0, 0.0, 20.0],
            [0.0, 0.0, 1.0, 30.0],
            [0.0, 0.0, 0.0, 1.0],
        ]

    def _post(self):
        return self.client.post(
            self.url,
            data=json.dumps({"affine": self.affine}),
            content_type="application/json",
        )

    @mock.patch("maxillo.views.metadata.get_object_storage")
    @mock.patch("maxillo.views.metadata.download_to_tempfile")
    def test_annotated_patient_gets_409_and_the_bytes_are_never_read(
        self, download, get_storage
    ):
        self._annotate()

        response = self._post()

        self.assertEqual(response.status_code, 409, response.content)
        body = response.json()
        self.assertTrue(body["raw_locked"])
        self.assertIn("voice captions", body["error"].lower())
        self.assertIn("scan orientation", body["error"])
        # The refusal happens before any object-storage work, so a locked case
        # costs nothing and cannot half-apply.
        download.assert_not_called()
        get_storage.assert_not_called()

    def test_the_refusal_precedes_the_missing_scan_check(self):
        """Order matters: a locked patient must not be told the scan is missing."""
        self.raw.delete()
        self._annotate()

        self.assertEqual(self._post().status_code, 409)

    @mock.patch("maxillo.views.patient_detail.artifact_exists", return_value=True)
    @mock.patch("maxillo.views.metadata.get_object_storage")
    @mock.patch("maxillo.views.metadata.download_to_tempfile")
    def test_an_unannotated_patient_still_reaches_the_write_path(
        self, download, get_storage, _exists
    ):
        """Only the gate is new; an open case must still get past it. The write
        itself is covered by ``maxillo.tests_metadata``."""
        response = self._post()

        self.assertNotEqual(response.status_code, 409)
        download.assert_called()


class ClassificationRespectsTheProjectTests(AnnotationGateTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse(
            "maxillo:update_classification",
            kwargs={"patient_id": self.patient.patient_id},
        )

    def _post(self, field="vertical", value="Normal"):
        return self.client.post(
            self.url,
            data=json.dumps({"field": field, "value": value}),
            content_type="application/json",
        )

    def _enable(self, *slugs):
        """The registry is seeded by ``common.0043``; the project's set is not
        -- ``0043`` wires every domain method, so a test that wants the method
        *off* has to say so."""
        self.project.annotation_methods.set(
            AnnotationMethod.objects.filter(slug__in=slugs)
        )

    def test_a_project_without_the_method_refuses_and_writes_nothing(self):
        self._enable()

        response = self._post()

        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("disabled for this project", response.json()["error"])
        self.assertFalse(Classification.objects.filter(patient=self.patient).exists())

    def test_either_slug_enables_it(self):
        for slug in ("classification", "bite_classification"):
            with self.subTest(slug=slug):
                self._enable(slug)

                response = self._post()

                self.assertEqual(response.status_code, 200, response.content)

    def test_a_user_outside_the_project_writes_nothing(self):
        """The permission check still runs first; the new gate did not displace
        it. An outsider is bounced by ``ActiveProfileMiddleware`` before the view
        is even entered, which is the pre-existing behaviour."""
        self._enable("classification")
        outsider = User.objects.create_user(username="gate-outsider", password="x")
        self.client.force_login(outsider)

        self.assertNotEqual(self._post().status_code, 200)
        self.assertFalse(Classification.objects.filter(patient=self.patient).exists())
