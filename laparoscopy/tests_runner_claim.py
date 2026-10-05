"""A laparoscopy job must be claimed with its own project slug, not "maxillo".

The runner writes outputs under ``<project_slug>/processed/<modality>/job_<id>``; the
maxillo-only lookup used to send every other domain's results to ``maxillo/``.
"""
from django.test import TestCase

from common.models import Job, Project
from common.uploads import entity_fk_kwargs
from laparoscopy.models import Folder, Patient
from maxillo.runner_api_service import _project_slug_for_job


class LaparoscopyJobProjectSlugTests(TestCase):
    def test_slug_follows_the_jobs_own_domain(self):
        project, _ = Project.objects.update_or_create(
            slug="laparoscopy", defaults={"name": "Laparoscopy", "domain": "laparoscopy"}
        )
        folder = Folder.objects.create(name="Cases", project=project)
        patient = Patient.objects.create(name="Case", folder=folder, project=project)
        job = Job.objects.create(
            modality_slug="video", status="pending", **entity_fk_kwargs(patient)
        )
        self.assertEqual(_project_slug_for_job(job), "laparoscopy")
