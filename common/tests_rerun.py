"""Queueing a patient's processing steps -- ``common.rerun``.

The property worth pinning is the one the feature exists for: a
``ProcessingStep`` registered *after* a patient was uploaded must still be
offered for that patient and must actually run the first time it is picked.
Before this was shared, only maxillo did that; brain and urology reset existing
``Job`` rows with a queryset ``update()``, which is silently a no-op for a step
that has never run.

The second property is that nothing here knows a domain by name: the patient's
own FK column comes from ``common.domains``, so a fifth domain needs no edit.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings

from common.domains import DOMAINS, fk_fields_for
from common.models import FileRegistry, Job, Modality, ProcessingStep, Project
from common.rerun import (
    _job_filter,
    bulk_rerun_steps,
    describe,
    rerun_steps_for_patient,
)
from common import modality_config as mc


def _project(domain):
    project, _ = Project.objects.get_or_create(
        slug=f"rerun-{domain}",
        defaults={"name": f"Rerun {domain}", "domain": domain},
    )
    return project


def _patient(domain="maxillo", **kwargs):
    from django.apps import apps

    Patient = apps.get_model(domain, "Patient")
    return Patient.objects.create(project=_project(domain), **kwargs)


def _file(patient, file_type, domain="maxillo"):
    patient_fk, _voice_fk = fk_fields_for(domain)
    return FileRegistry.objects.create(
        file_type=file_type,
        file_path=f"p/{file_type}",
        file_size=1,
        file_hash="h",
        domain=domain,
        **{patient_fk: patient},
    )


@override_settings(RUNNER_QUEUE_BY_MODALITY=None)
@patch("common.signals.celery_app.send_task")
class NewStepReachesOldPatientsTests(TestCase):
    """The point of the feature: register an algorithm, run it on the back catalogue."""

    def _ios_step(self):
        """A root step registered *now*, for a modality the patient already has."""
        modality = Modality.objects.create(slug="ios", name="IOS")
        return ProcessingStep.objects.create(
            modality=modality, slug="ios", name="IOS Orientation"
        )

    def test_a_step_registered_after_upload_is_offered(self, _send_task):
        patient = _patient()
        rows = [_file(patient, "ios_raw_upper")]
        # The patient predates the step: it has the scan, and no job at all.
        self._ios_step()
        self.assertFalse(Job.objects.filter(patient=patient).exists())

        offered = [s["slug"] for s in mc.rerunnable_steps_for_patient(rows, [], patient=patient)]
        self.assertIn("ios", offered)

    def test_picking_it_creates_the_job_and_queues_it(self, _send_task):
        patient = _patient()
        _file(patient, "ios_raw_upper")
        self._ios_step()

        result = rerun_steps_for_patient(patient, ["ios"])

        job = Job.objects.get(patient=patient, modality_slug="ios")
        self.assertEqual(job.status, "pending")
        self.assertIn("ios", result["created"])
        self.assertIn("ios", result["updated"])
        self.assertEqual(result["not_found"], [])

    def test_a_step_that_already_ran_is_reset_not_duplicated(self, _send_task):
        patient = _patient()
        _file(patient, "ios_raw_upper")
        self._ios_step()
        Job.objects.create(
            modality_slug="ios", status="failed", error_logs="boom",
            worker_id="w1", patient=patient, domain="maxillo",
        )

        result = rerun_steps_for_patient(patient, ["ios"])

        self.assertEqual(Job.objects.filter(patient=patient, modality_slug="ios").count(), 1)
        job = Job.objects.get(patient=patient, modality_slug="ios")
        self.assertEqual(job.status, "pending")
        self.assertEqual(job.error_logs, "")
        self.assertEqual(job.worker_id, "")
        self.assertEqual(result["created"], [])
        self.assertEqual(result["updated"], ["ios"])

    def test_a_step_the_patient_has_no_input_for_is_reported_not_invented(self, _send_task):
        patient = _patient()  # no files at all
        self._ios_step()

        result = rerun_steps_for_patient(patient, ["ios"])

        self.assertEqual(result["updated"], [])
        self.assertEqual(result["not_found"], ["ios"])
        self.assertFalse(Job.objects.filter(patient=patient).exists())


@override_settings(RUNNER_QUEUE_BY_MODALITY=None)
@patch("common.signals.celery_app.send_task")
class RequestShapeTests(TestCase):
    def test_captions_are_not_pipeline_steps(self, _send_task):
        patient = _patient()
        result = rerun_steps_for_patient(patient, ["audio", "voice"])
        self.assertEqual(result, {"updated": [], "created": [], "not_found": []})

    def test_blank_and_duplicate_slugs_collapse(self, _send_task):
        patient = _patient()
        result = rerun_steps_for_patient(patient, ["  ", None, "ios", "ios"])
        # 'ios' resolves to nothing here, but it is asked for exactly once.
        self.assertEqual(result["not_found"], ["ios"])

    def test_describe_names_what_happened(self, _send_task):
        self.assertEqual(
            describe({"created": ["a"], "updated": ["a"], "not_found": ["b"]}),
            "Created missing job for: a; Updated: a; No job found for: b",
        )
        self.assertEqual(
            describe({"created": [], "updated": [], "not_found": []}), "No changes made"
        )


class DomainResolutionTests(TestCase):
    """No domain is named in common/rerun.py; the registry supplies the column."""

    def test_every_domain_resolves_to_its_own_patient_fk(self):
        for domain in sorted(DOMAINS):
            with self.subTest(domain=domain):
                patient = _patient(domain)
                job_filter = _job_filter(patient)
                patient_fk, _voice_fk = fk_fields_for(domain)
                self.assertEqual(job_filter["domain"], domain)
                self.assertEqual(job_filter[patient_fk], patient)
                # Exactly the domain key and that one FK -- the other domains'
                # parallel columns are left out, not set to None.
                self.assertEqual(set(job_filter), {"domain", patient_fk})


@override_settings(RUNNER_QUEUE_BY_MODALITY=None)
@patch("common.signals.celery_app.send_task")
class BulkRerunTests(TestCase):
    def test_counts_are_aggregated_per_step(self, _send_task):
        modality = Modality.objects.create(slug="ios", name="IOS")
        ProcessingStep.objects.create(modality=modality, slug="ios", name="IOS Orientation")

        with_scan = _patient()
        _file(with_scan, "ios_raw_upper")
        without_scan = _patient()

        result = bulk_rerun_steps([with_scan, without_scan], ["ios"])

        self.assertEqual(result["requested"], ["ios"])
        self.assertEqual(result["updated_pairs"], 1)
        self.assertEqual(result["not_found_pairs"], 1)
        self.assertEqual(result["updated_by_modality"], {"ios": 1})
        self.assertEqual(result["not_found_by_modality"], {"ios": 1})
        self.assertEqual(result["created_slugs"], ["ios"])
