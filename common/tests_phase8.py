"""Phase 8 — brain API auth hardening + bulk job re-submit.

Locks two things:
  * the brain processing API is no longer anonymous, and the unauthenticated
    runner duplicates are gone (external runners use the single token-authed
    contract under /api/runner/...);
  * ``resubmit_jobs`` creates a pending Job (with correct domain + input) for a
    patient that has a raw file but no job, is idempotent without
    ``--include-existing``, and re-pends with it.
"""

from io import StringIO

from django.apps import apps
from django.core.management import call_command
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from common.models import FileRegistry, Job, Modality
from common.uploads import domain_for_patient


class BrainApiAuthTests(TestCase):
    def test_runner_routes_removed(self):
        for name in (
            "brain:api_runner_claim_job",
            "brain:api_runner_complete_job",
            "brain:api_runner_fail_job",
        ):
            with self.assertRaises(NoReverseMatch):
                reverse(name, args=[1])

    def test_global_secure_runner_route_present(self):
        self.assertEqual(reverse("api:api_runner_claim_job", args=[1]), "/api/runner/jobs/1/claim/")

    def test_brain_endpoints_reject_anonymous(self):
        # login_required -> redirect to login (302), never 200.
        for url in (
            reverse("brain:api_serve_file", args=[1]),
            reverse("brain:api_get_file_registry"),
            reverse("brain:api_get_job_status", args=[1]),
        ):
            self.assertEqual(self.client.get(url).status_code, 302)


class ResubmitJobsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        BF = apps.get_model("brain", "Folder")
        BP = apps.get_model("brain", "Patient")
        cls.folder = BF.objects.create(name="F")
        cls.patient = BP.objects.create(name="P")
        cls.patient.folders.add(cls.folder)
        cls.modality = Modality.objects.create(slug="braintumor_mri_flair", name="FLAIR")
        FileRegistry.objects.create(
            domain="brain", brain_patient=cls.patient, modality=cls.modality,
            file_type="braintumor_mri_flair_raw", file_path="brain/raw/flair.nii.gz",
            file_hash="h", file_size=5,
        )

    def _run(self, *extra):
        out = StringIO()
        call_command("resubmit_jobs", "--domain", "brain",
                     "--modality", "braintumor_mri_flair", *extra, stdout=out)
        return out.getvalue()

    def test_domain_for_patient_brain(self):
        self.assertEqual(domain_for_patient(self.patient), "brain")

    def test_creates_job_for_missing(self):
        self._run()
        job = Job.objects.get(brain_patient=self.patient, modality_slug="braintumor_mri_flair")
        self.assertEqual(job.domain, "brain")
        self.assertEqual(job.status, "pending")
        self.assertEqual(job.input_files, {"input": "brain/raw/flair.nii.gz"})

    def test_idempotent_without_include_existing(self):
        self._run()
        self._run()
        self.assertEqual(
            Job.objects.filter(brain_patient=self.patient, modality_slug="braintumor_mri_flair").count(),
            1,
        )

    def test_include_existing_repends(self):
        self._run()
        job = Job.objects.get(brain_patient=self.patient, modality_slug="braintumor_mri_flair")
        job.status = "completed"
        job.save()
        self._run("--include-existing")
        job.refresh_from_db()
        self.assertEqual(job.status, "pending")

    def test_dry_run_writes_nothing(self):
        self._run("--dry-run")
        self.assertFalse(Job.objects.filter(brain_patient=self.patient).exists())
