import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from common.models import FileRegistry, Modality, ProcessingStep, Project, ProjectAccess
from maxillo.models import Folder, Patient


def _step(modality, slug, name):
    return ProcessingStep.objects.create(
        modality=modality, name=name, slug=slug, is_enabled=True
    )


class MaxilloRerunStepsBase(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.get_or_create(slug="maxillo", defaults={"name": "Maxillo"})
        self.user = User.objects.create_user(username="maxillo-rerun-admin", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

        self.ios_modality = Modality.objects.create(slug="ios", name="IOS")
        self.cbct_modality = Modality.objects.create(slug="cbct", name="CBCT")
        self.project.modalities.add(self.ios_modality, self.cbct_modality)
        self.folder = Folder.objects.create(name="General", project=self.project)
        self.ios_step = _step(self.ios_modality, "ios", "IOS Orientation")
        self.cbct_step = _step(self.cbct_modality, "cbct", "CBCT Segmentation")
        self.landmarks_step = _step(self.ios_modality, "ios-landmarks", "IOS Landmarks")
        self.landmarks_step.depends_on.add(self.ios_step)
        self.bite_step = _step(self.ios_modality, "ios-bite-classification", "IOS Bite Classification")
        self.bite_step.depends_on.add(self.landmarks_step)

    def _ios_patient(self):
        patient = Patient.objects.create(name="IOS Scan", project=self.project, folder=self.folder)
        FileRegistry.objects.create(
            patient=patient,
            domain="maxillo",
            file_type="ios_raw_upper",
            file_path="maxillo/upper.stl",
            file_size=1,
            file_hash="0" * 64,
        )
        FileRegistry.objects.create(
            patient=patient,
            domain="maxillo",
            file_type="ios_raw_lower",
            file_path="maxillo/lower.stl",
            file_size=1,
            file_hash="0" * 64,
        )
        return patient


class RerunButtonStepsTests(MaxilloRerunStepsBase):
    def test_ios_patient_row_exposes_step_slugs(self):
        patient = self._ios_patient()

        response = self.client.get(reverse("maxillo:patient_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'data-scan-id="{patient.patient_id}"',
        )
        self.assertContains(
            response,
            'data-available-steps="ios,ios-landmarks,ios-bite-classification"',
        )

    def test_non_ios_patient_has_no_available_steps(self):
        patient = Patient.objects.create(name="Plain", project=self.project, folder=self.folder)

        response = self.client.get(reverse("maxillo:patient_list"))
        self.assertContains(
            response,
            f'data-scan-id="{patient.patient_id}"',
        )
        self.assertContains(response, 'data-available-steps=""')

    def test_label_script_uses_processing_step_names(self):
        response = self.client.get(reverse("maxillo:patient_list"))
        self.assertContains(response, '"ios": "IOS Orientation"')
        self.assertContains(response, '"ios-landmarks": "IOS Landmarks"')
        self.assertContains(response, '"ios-bite-classification": "IOS Bite Classification"')
        self.assertContains(response, '"cbct": "CBCT Segmentation"')

    def test_no_django_comment_text_leaks_into_page(self):
        # Multi-line {# ... #} comments are not supported on Django 5.x and would
        # render as literal text; the rerun labels comment must stay hidden.
        response = self.client.get(reverse("maxillo:patient_list"))
        self.assertNotContains(response, "{#")
        self.assertNotContains(response, "human label map consumed")


@patch("common.signals.celery_app.send_task")
class RerunProcessingCreatesJobsTests(MaxilloRerunStepsBase):
    def test_rerun_creates_missing_step_jobs_for_existing_patient(self, _send_task):
        from common.models import Job

        patient = self._ios_patient()
        # Existing upload source job; the downstream steps were registered later.
        # In-flight so the created dependents stay 'dependency'.
        Job.objects.create(
            modality_slug="ios", status="processing", patient=patient, domain="maxillo"
        )

        response = self.client.post(
            reverse("maxillo:rerun_processing", args=[patient.patient_id]),
            data=json.dumps({"jobs": ["ios-bite-classification"]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["created"], ["ios-bite-classification", "ios-landmarks"])

        landmarks = Job.objects.get(patient=patient, modality_slug="ios-landmarks")
        bite = Job.objects.get(patient=patient, modality_slug="ios-bite-classification")
        self.assertEqual(landmarks.status, "dependency")
        self.assertEqual(bite.status, "dependency")
        self.assertIn(landmarks, bite.dependencies.all())

    def test_rerun_resets_existing_job_to_pending(self, _send_task):
        from common.models import Job

        patient = self._ios_patient()
        Job.objects.create(
            modality_slug="ios", status="completed", patient=patient, domain="maxillo"
        )

        response = self.client.post(
            reverse("maxillo:rerun_processing", args=[patient.patient_id]),
            data=json.dumps({"jobs": ["ios"]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertIn("ios", data["updated"])
        self.assertEqual(data["created"], [])
        self.assertEqual(
            Job.objects.get(patient=patient, modality_slug="ios").status,
            "pending",
        )
