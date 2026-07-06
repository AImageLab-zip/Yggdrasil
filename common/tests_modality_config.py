from unittest.mock import patch

from django.test import TestCase, override_settings

from common.models import Modality, ProcessingStep
from common import modality_config as mc


def _modality(slug, name=None):
    return Modality.objects.create(slug=slug, name=name or slug.upper())


def _step(modality, slug=None, **kwargs):
    """Create a ProcessingStep. Defaults to the modality's *root* step
    (slug == modality slug)."""
    return ProcessingStep.objects.create(
        modality=modality,
        name=kwargs.pop("name", slug or modality.slug),
        slug=slug or modality.slug,
        **kwargs,
    )


class ModalityConfigAccessorTests(TestCase):
    # Neutralize any local .env env-routing so fallbacks are deterministic.
    @override_settings(RUNNER_QUEUE_BY_MODALITY=None)
    def test_requires_processing_legacy_fallback_when_no_step(self):
        self.assertFalse(mc.modality_requires_processing("panoramic"))
        self.assertFalse(mc.modality_requires_processing("teleradiography"))
        self.assertFalse(mc.modality_requires_processing("rawzip"))
        self.assertTrue(mc.modality_requires_processing("ios"))

    def test_requires_processing_step_overrides_legacy(self):
        m = _modality("panoramic")
        _step(m, is_enabled=True)
        self.assertTrue(mc.modality_requires_processing("panoramic"))

        m2 = _modality("ios")
        _step(m2, is_enabled=False)
        self.assertFalse(mc.modality_requires_processing("ios"))

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"other": "q"})
    def test_is_enabled_env_fallback_when_no_step(self):
        # env map present but slug absent => disabled (legacy behavior)
        self.assertFalse(mc.modality_is_enabled("demo"))

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"other": "q"})
    def test_is_enabled_step_overrides_env(self):
        m = _modality("demo")
        _step(m, is_enabled=True)
        # env would disable (slug absent), but the enabled step wins
        self.assertTrue(mc.modality_is_enabled("demo"))

        m2 = _modality("demo2")
        _step(m2, is_enabled=False)
        self.assertFalse(mc.modality_is_enabled("demo2"))

    def test_queue_override_for(self):
        self.assertIsNone(mc.queue_override_for("demo"))
        m = _modality("demo")
        step = _step(m, queue_name="")
        self.assertIsNone(mc.queue_override_for("demo"))
        step.queue_name = "special-q"
        step.save()
        self.assertEqual(mc.queue_override_for("demo"), "special-q")

    @override_settings(RUNNER_QUEUE_BY_MODALITY=None)
    def test_is_blocking_defaults_to_requires_processing_when_no_step(self):
        self.assertTrue(mc.modality_is_blocking("ios"))
        self.assertFalse(mc.modality_is_blocking("panoramic"))

    def test_is_blocking_step_value(self):
        m = _modality("ios")
        _step(m, is_blocking=False)
        self.assertFalse(mc.modality_is_blocking("ios"))

    def test_discard_raw_defaults_false_without_step(self):
        self.assertFalse(mc.modality_discard_raw("panoramic"))

    def test_discard_raw_step_value(self):
        m = _modality("panoramic")
        step = _step(m, discard_raw=False)
        self.assertFalse(mc.modality_discard_raw("panoramic"))
        step.discard_raw = True
        step.save()
        self.assertTrue(mc.modality_discard_raw("panoramic"))


@override_settings(RUNNER_QUEUE_BY_MODALITY=None)
class RawFileHiddenTests(TestCase):
    """raw_file_hidden gates raw inputs by discard_raw / blocking-until-processed."""

    def _raw(self, patient, file_type="panoramic_raw", path="p/raw.png"):
        from common.models import FileRegistry
        return FileRegistry.objects.create(
            file_type=file_type, file_path=path, file_size=1, file_hash="h",
            patient=patient,
        )

    def _processed(self, patient, file_type="panoramic_processed", path="p/proc.png"):
        from common.models import FileRegistry
        return FileRegistry.objects.create(
            file_type=file_type, file_path=path, file_size=1, file_hash="h",
            patient=patient,
        )

    def test_no_step_never_hidden(self):
        from maxillo.models import Patient
        patient = Patient.objects.create()
        self.assertFalse(mc.raw_file_hidden(self._raw(patient)))

    def test_non_raw_file_never_hidden(self):
        m = _modality("panoramic")
        _step(m, discard_raw=True, is_blocking=True)
        from maxillo.models import Patient
        patient = Patient.objects.create()
        self.assertFalse(mc.raw_file_hidden(self._processed(patient)))

    def test_discard_raw_hides_even_when_processed_exists(self):
        m = _modality("panoramic")
        _step(m, discard_raw=True)
        from maxillo.models import Patient
        patient = Patient.objects.create()
        self._processed(patient)
        self.assertTrue(mc.raw_file_hidden(self._raw(patient)))

    def test_blocking_hides_raw_until_processed_exists(self):
        m = _modality("panoramic")
        _step(m, is_blocking=True, discard_raw=False)
        from maxillo.models import Patient
        patient = Patient.objects.create()
        raw = self._raw(patient)
        # No processed output yet -> blocked/hidden.
        self.assertTrue(mc.raw_file_hidden(raw))
        # Processed output present -> gate lifts.
        self._processed(patient)
        self.assertFalse(mc.raw_file_hidden(raw))

    def test_nonblocking_non_discard_shows_raw(self):
        m = _modality("panoramic")
        _step(m, is_blocking=False, discard_raw=False)
        from maxillo.models import Patient
        patient = Patient.objects.create()
        self.assertFalse(mc.raw_file_hidden(self._raw(patient)))


@override_settings(RUNNER_QUEUE_BY_MODALITY=None)
class CreateStepJobsTests(TestCase):
    """create_step_jobs spawns the downstream ProcessingStep pipeline, including
    cross-modality dependents (the generalized ios -> bite wiring)."""

    def _ios_bite(self):
        ios = _modality("ios")
        bite = _modality("bite_classification", "Bite")
        ios_step = _step(ios)   # root ios
        bite_step = _step(bite)  # root bite (slug == 'bite_classification')
        bite_step.depends_on.add(ios_step)
        return ios_step, bite_step

    @patch("common.signals.celery_app.send_task")
    def test_ios_source_job_spawns_bite_dependency(self, _send):
        from common.models import Job
        from maxillo.models import Patient
        from common.uploads import create_step_jobs

        self._ios_bite()
        patient = Patient.objects.create()
        # In-flight (not completed) so the dependent stays in 'dependency' status.
        ios_job = Job.objects.create(
            modality_slug="ios", status="processing", patient=patient, domain="maxillo"
        )
        created = create_step_jobs(ios_job)
        self.assertEqual([j.modality_slug for j in created], ["bite_classification"])
        bite = created[0]
        self.assertEqual(bite.status, "dependency")
        self.assertEqual(bite.patient_id, patient.pk)
        self.assertIn(ios_job, bite.dependencies.all())

    def test_no_source_job_creates_nothing(self):
        from common.uploads import create_step_jobs

        self.assertEqual(create_step_jobs(None), [])

    @patch("common.signals.celery_app.send_task")
    def test_modality_without_steps_creates_nothing(self, _send):
        from common.models import Job
        from maxillo.models import Patient
        from common.uploads import create_step_jobs

        _modality("ios")  # no steps declared
        patient = Patient.objects.create()
        ios_job = Job.objects.create(
            modality_slug="ios", status="processing", patient=patient, domain="maxillo"
        )
        self.assertEqual(create_step_jobs(ios_job), [])

    @patch("common.signals.celery_app.send_task")
    def test_dependency_output_flows_into_dependent_input(self, _send):
        """The core new capability: a completed prerequisite's output_files
        become the dependent step's input_files when it unblocks."""
        from common.models import Job
        from maxillo.models import Patient
        from common.uploads import create_step_jobs

        self._ios_bite()
        patient = Patient.objects.create()
        ios_job = Job.objects.create(
            modality_slug="ios", status="pending", patient=patient, domain="maxillo"
        )
        (bite,) = create_step_jobs(ios_job)
        self.assertEqual(bite.status, "dependency")

        ios_job.mark_completed({"aligned": "path/to/aligned.obj"})
        bite.refresh_from_db()
        self.assertEqual(bite.status, "pending")
        self.assertEqual(bite.input_files.get("ios"), {"aligned": "path/to/aligned.obj"})


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
        step = _step(m, is_enabled=True, is_blocking=True)
        patient = self._maxillo_patient_with_job("demo")
        self.assertEqual(patient._processing_status("demo"), "processing")

        step.is_blocking = False
        step.save()
        self.assertEqual(patient._processing_status("demo"), "processed")

    def test_brain_blocking_vs_nonblocking(self, _send_task):
        m = _modality("demo")
        step = _step(m, is_enabled=True, is_blocking=True)
        patient = self._brain_patient_with_job("demo")
        self.assertEqual(patient._processing_status("demo"), "processing")

        step.is_blocking = False
        step.save()
        self.assertEqual(patient._processing_status("demo"), "processed")
