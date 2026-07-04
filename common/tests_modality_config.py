from unittest.mock import patch

from django.test import TestCase, override_settings

from common.models import Modality, ModalityProcessingConfig
from common import modality_config as mc


def _modality(slug, name=None):
    return Modality.objects.create(slug=slug, name=name or slug.upper())


class ModalityConfigAccessorTests(TestCase):
    # Neutralize any local .env env-routing so fallbacks are deterministic.
    @override_settings(RUNNER_QUEUE_BY_MODALITY=None)
    def test_requires_processing_legacy_fallback_when_no_row(self):
        self.assertFalse(mc.modality_requires_processing("panoramic"))
        self.assertFalse(mc.modality_requires_processing("teleradiography"))
        self.assertFalse(mc.modality_requires_processing("rawzip"))
        self.assertTrue(mc.modality_requires_processing("ios"))

    def test_requires_processing_row_overrides_legacy(self):
        m = _modality("panoramic")
        ModalityProcessingConfig.objects.create(modality=m, requires_processing=True)
        self.assertTrue(mc.modality_requires_processing("panoramic"))

        m2 = _modality("ios")
        ModalityProcessingConfig.objects.create(modality=m2, requires_processing=False)
        self.assertFalse(mc.modality_requires_processing("ios"))

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"other": "q"})
    def test_is_enabled_env_fallback_when_no_row(self):
        # env map present but slug absent => disabled (legacy behavior)
        self.assertFalse(mc.modality_is_enabled("demo"))

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"other": "q"})
    def test_is_enabled_row_overrides_env(self):
        m = _modality("demo")
        ModalityProcessingConfig.objects.create(modality=m, is_enabled=True)
        # env would disable (slug absent), but the DB row enables it
        self.assertTrue(mc.modality_is_enabled("demo"))

        m2 = _modality("demo2")
        ModalityProcessingConfig.objects.create(modality=m2, is_enabled=False)
        self.assertFalse(mc.modality_is_enabled("demo2"))

    def test_queue_override_for(self):
        self.assertIsNone(mc.queue_override_for("demo"))
        m = _modality("demo")
        cfg = ModalityProcessingConfig.objects.create(modality=m, queue_name="")
        self.assertIsNone(mc.queue_override_for("demo"))
        cfg.queue_name = "special-q"
        cfg.save()
        self.assertEqual(mc.queue_override_for("demo"), "special-q")

    @override_settings(RUNNER_QUEUE_BY_MODALITY=None)
    def test_is_blocking_defaults_to_requires_processing_when_no_row(self):
        self.assertTrue(mc.modality_is_blocking("ios"))
        self.assertFalse(mc.modality_is_blocking("panoramic"))

    def test_is_blocking_row_value(self):
        m = _modality("ios")
        ModalityProcessingConfig.objects.create(modality=m, is_blocking=False)
        self.assertFalse(mc.modality_is_blocking("ios"))


class DependentSlugsTests(TestCase):
    def test_config_driven_dependency(self):
        ios = _modality("ios")
        bite = _modality("bite_classification", "Bite")
        cfg = ModalityProcessingConfig.objects.create(modality=bite, is_enabled=True)
        cfg.depends_on.add(ios)
        self.assertEqual(mc.dependent_slugs_of("ios"), ["bite_classification"])

    def test_legacy_fallback_when_no_rows(self):
        _modality("ios")
        _modality("bite_classification", "Bite")
        # No config rows at all => legacy ios->bite dependency applies.
        self.assertEqual(mc.dependent_slugs_of("ios"), ["bite_classification"])

    def test_admin_cleared_dependency_respected(self):
        _modality("ios")
        bite = _modality("bite_classification", "Bite")
        # bite has a config row but no depends_on => admin intentionally cleared it.
        ModalityProcessingConfig.objects.create(modality=bite, is_enabled=True)
        self.assertEqual(mc.dependent_slugs_of("ios"), [])

    def test_disabled_dependent_excluded(self):
        ios = _modality("ios")
        bite = _modality("bite_classification", "Bite")
        cfg = ModalityProcessingConfig.objects.create(modality=bite, is_enabled=False)
        cfg.depends_on.add(ios)
        self.assertEqual(mc.dependent_slugs_of("ios"), [])


@override_settings(RUNNER_QUEUE_BY_MODALITY=None)
class CreateDependentJobsTests(TestCase):
    """The generalized replacement for the hardcoded ios -> bite wiring."""

    def test_legacy_ios_creates_bite_dependency(self):
        from common.models import Job
        from maxillo.models import Patient
        from maxillo.file_utils import create_dependent_jobs

        patient = Patient.objects.create()
        # In-flight (not completed) so the dependent stays in 'dependency' status.
        ios_job = Job.objects.create(
            modality_slug="ios", status="processing", patient=patient, domain="maxillo"
        )
        created = create_dependent_jobs(patient, ios_job, "ios")
        self.assertEqual([j.modality_slug for j in created], ["bite_classification"])
        bite = created[0]
        self.assertEqual(bite.status, "dependency")
        self.assertEqual(bite.patient_id, patient.pk)
        self.assertIn(ios_job, bite.dependencies.all())

    def test_no_source_job_creates_nothing(self):
        from maxillo.models import Patient
        from maxillo.file_utils import create_dependent_jobs

        patient = Patient.objects.create()
        self.assertEqual(create_dependent_jobs(patient, None, "ios"), [])


# send_task is mocked so pending-job creation doesn't hit a real broker.
@override_settings(RUNNER_QUEUE_BY_MODALITY=None)
@patch("common.signals.celery_app.send_task")
class ProcessingStatusBlockingTests(TestCase):
    """is_blocking gates whether an in-flight job shows patient 'processing'."""

    def _maxillo_patient_with_job(self, slug, status="pending"):
        from common.models import Job
        from maxillo.models import Patient
        patient = Patient.objects.create()
        Job.objects.create(modality_slug=slug, status=status, patient=patient, domain="maxillo")
        return patient

    def _brain_patient_with_job(self, slug, status="pending"):
        from common.models import Job
        from brain.models import Patient as BrainPatient
        patient = BrainPatient.objects.create()
        Job.objects.create(modality_slug=slug, status=status, brain_patient=patient, domain="brain")
        return patient

    def test_maxillo_blocking_vs_nonblocking(self, _send_task):
        m = _modality("demo")
        cfg = ModalityProcessingConfig.objects.create(
            modality=m, is_enabled=True, is_blocking=True
        )
        patient = self._maxillo_patient_with_job("demo")
        self.assertEqual(patient._processing_status("demo"), "processing")

        cfg.is_blocking = False
        cfg.save()
        self.assertEqual(patient._processing_status("demo"), "processed")

    def test_brain_blocking_vs_nonblocking(self, _send_task):
        m = _modality("demo")
        cfg = ModalityProcessingConfig.objects.create(
            modality=m, is_enabled=True, is_blocking=True
        )
        patient = self._brain_patient_with_job("demo")
        self.assertEqual(patient._processing_status("demo"), "processing")

        cfg.is_blocking = False
        cfg.save()
        self.assertEqual(patient._processing_status("demo"), "processed")
