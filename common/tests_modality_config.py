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


def _project(domain="maxillo"):
    """A throwaway project for the domain. Patients are project-scoped."""
    from common.models import Project

    project, _ = Project.objects.get_or_create(
        slug=f"modality-config-{domain}",
        defaults={"name": f"Modality Config {domain}", "domain": domain},
    )
    return project


def _patient(**kwargs):
    from maxillo.models import Patient

    return Patient.objects.create(project=_project(), **kwargs)


def _brain_patient(**kwargs):
    from brain.models import Patient

    return Patient.objects.create(project=_project("brain"), **kwargs)


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

    def test_prefer_processed_for_viewer_defaults_false_without_step(self):
        self.assertFalse(mc.modality_prefers_processed_for_viewer("ios"))

    def test_prefer_processed_for_viewer_uses_step_value(self):
        m = _modality("ios")
        step = _step(m, prefer_processed_for_viewer=False)
        self.assertFalse(mc.modality_prefers_processed_for_viewer("ios"))
        step.prefer_processed_for_viewer = True
        step.save()
        self.assertTrue(mc.modality_prefers_processed_for_viewer("ios"))


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
        patient = _patient()
        self.assertFalse(mc.raw_file_hidden(self._raw(patient)))

    def test_non_raw_file_never_hidden(self):
        m = _modality("panoramic")
        _step(m, discard_raw=True, is_blocking=True)
        patient = _patient()
        self.assertFalse(mc.raw_file_hidden(self._processed(patient)))

    def test_discard_raw_hides_even_when_processed_exists(self):
        m = _modality("panoramic")
        _step(m, discard_raw=True)
        patient = _patient()
        self._processed(patient)
        self.assertTrue(mc.raw_file_hidden(self._raw(patient)))

    def test_blocking_hides_raw_until_processed_exists(self):
        m = _modality("panoramic")
        _step(m, is_blocking=True, discard_raw=False)
        patient = _patient()
        raw = self._raw(patient)
        # No processed output yet -> blocked/hidden.
        self.assertTrue(mc.raw_file_hidden(raw))
        # Processed output present -> gate lifts.
        self._processed(patient)
        self.assertFalse(mc.raw_file_hidden(raw))

    def test_nonblocking_non_discard_shows_raw(self):
        m = _modality("panoramic")
        _step(m, is_blocking=False, discard_raw=False)
        patient = _patient()
        self.assertFalse(mc.raw_file_hidden(self._raw(patient)))

    def test_discard_raw_recognizes_ios_arch_file_types(self):
        m = _modality("ios")
        _step(m, discard_raw=True)
        patient = _patient()

        upper = self._raw(patient, "ios_raw_upper", "p/upper.stl")
        lower = self._raw(patient, "ios_raw_lower", "p/lower.stl")

        self.assertTrue(mc.raw_file_hidden(upper))
        self.assertTrue(mc.raw_file_hidden(lower))


@override_settings(RUNNER_QUEUE_BY_MODALITY=None)
class RerunnableStepsTests(TestCase):
    """rerunnable_steps_for_patient resolves the enabled ProcessingStep DAG a
    patient's raw inputs unlock (IOS -> IOS Landmarks -> IOS Bite Classification)."""

    def _ios_pipeline(self):
        """Real-world IOS chain: root 'ios', then landmarks, then bite."""
        ios = _modality("ios")
        cbct = _modality("cbct")
        ios_step = _step(ios, slug="ios", name="IOS Orientation")
        cbct_step = _step(cbct, slug="cbct", name="CBCT Segmentation")
        landmarks = _step(ios, slug="ios-landmarks", name="IOS Landmarks")
        landmarks.depends_on.add(ios_step)
        bite = _step(ios, slug="ios-bite-classification", name="IOS Bite Classification")
        bite.depends_on.add(landmarks)
        return ios_step, cbct_step, landmarks, bite

    def _raw(self, patient, file_type, path):
        from common.models import FileRegistry
        return FileRegistry.objects.create(
            file_type=file_type, file_path=path, file_size=1, file_hash="h",
            patient=patient, domain="maxillo",
        )

    def test_ios_patient_exposes_full_step_chain(self):
        self._ios_pipeline()
        patient = _patient()
        self._raw(patient, "ios_raw_upper", "p/upper.stl")
        self._raw(patient, "ios_raw_lower", "p/lower.stl")

        steps = mc.rerunnable_steps_for_patient(list(patient.files.all()), [])
        self.assertEqual(
            [s["slug"] for s in steps],
            ["ios", "ios-landmarks", "ios-bite-classification"],
        )
        self.assertEqual(
            [s["name"] for s in steps],
            ["IOS Orientation", "IOS Landmarks", "IOS Bite Classification"],
        )

    def test_no_ios_input_means_no_ios_steps(self):
        self._ios_pipeline()
        patient = _patient()

        steps = mc.rerunnable_steps_for_patient(list(patient.files.all()), [])
        self.assertEqual(steps, [])

    def test_cbct_patient_exposes_only_cbct(self):
        self._ios_pipeline()
        patient = _patient()
        self._raw(patient, "cbct_raw", "p/vol.nii")

        steps = mc.rerunnable_steps_for_patient(list(patient.files.all()), [])
        self.assertEqual([s["slug"] for s in steps], ["cbct"])
        self.assertEqual(steps[0]["name"], "CBCT Segmentation")

    def test_disabled_downstream_step_is_skipped(self):
        ios_step, _cbct_step, landmarks, _bite = self._ios_pipeline()
        landmarks.is_enabled = False
        landmarks.save()
        patient = _patient()
        self._raw(patient, "ios_raw_upper", "p/upper.stl")
        self._raw(patient, "ios_raw_lower", "p/lower.stl")

        steps = mc.rerunnable_steps_for_patient(list(patient.files.all()), [])
        self.assertEqual([s["slug"] for s in steps], ["ios"])

    def test_disabled_root_blocks_dependents(self):
        ios_step, _cbct_step, _landmarks, _bite = self._ios_pipeline()
        ios_step.is_enabled = False
        ios_step.save()
        patient = _patient()
        self._raw(patient, "ios_raw_upper", "p/upper.stl")
        self._raw(patient, "ios_raw_lower", "p/lower.stl")

        steps = mc.rerunnable_steps_for_patient(list(patient.files.all()), [])
        self.assertEqual(steps, [])

    def test_falls_back_to_legacy_modalities_when_no_steps(self):
        # No ProcessingStep rows at all -> legacy per-modality behavior.
        steps = mc.rerunnable_steps_for_patient(
            [],
            [{"slug": "cbct", "name": "CBCT", "label": "", "status": "processed"}],
        )
        self.assertEqual(steps, [{"slug": "cbct", "name": "CBCT"}])

        steps = mc.rerunnable_steps_for_patient(
            [],
            [
                {"slug": "cbct", "name": "CBCT", "label": "", "status": "absent"},
                {"slug": "ios", "name": "IOS", "label": "IOS", "status": "processed"},
            ],
        )
        self.assertEqual(steps, [{"slug": "ios", "name": "IOS"}])

    def test_rerun_step_labels_prefers_step_names(self):
        self._ios_pipeline()
        labels = mc.rerun_step_labels([], [])
        self.assertEqual(labels["ios"], "IOS Orientation")
        self.assertEqual(labels["ios-landmarks"], "IOS Landmarks")
        self.assertEqual(labels["ios-bite-classification"], "IOS Bite Classification")
        self.assertEqual(labels["cbct"], "CBCT Segmentation")


@override_settings(RUNNER_QUEUE_BY_MODALITY=None)
@patch("common.signals.celery_app.send_task")
class EnsureStepJobsTests(TestCase):
    """ensure_step_jobs_for_patient creates missing downstream jobs (with
    dependency wiring) so newly-registered steps run on existing patients."""

    def _ios_pipeline(self):
        ios = _modality("ios")
        ios_step = _step(ios, slug="ios", name="IOS Orientation")
        landmarks = _step(ios, slug="ios-landmarks", name="IOS Landmarks")
        landmarks.depends_on.add(ios_step)
        bite = _step(ios, slug="ios-bite-classification", name="IOS Bite Classification")
        bite.depends_on.add(landmarks)
        return ios_step, landmarks, bite

    def test_creates_missing_downstream_jobs_for_new_step(self, _send_task):
        from common.models import Job
        from common.uploads import ensure_step_jobs_for_patient

        self._ios_pipeline()
        patient = _patient()
        # In-flight (not completed) so the created dependents stay 'dependency'.
        ios_job = Job.objects.create(
            modality_slug="ios", status="processing", patient=patient, domain="maxillo"
        )

        created = ensure_step_jobs_for_patient(patient, ["ios-bite-classification"])
        self.assertEqual(
            sorted(j.modality_slug for j in created),
            ["ios-bite-classification", "ios-landmarks"],
        )

        by_slug = {j.modality_slug: j for j in created}
        landmarks = by_slug["ios-landmarks"]
        bite = by_slug["ios-bite-classification"]
        self.assertEqual(landmarks.status, "dependency")
        self.assertEqual(bite.status, "dependency")
        self.assertIn(ios_job, landmarks.dependencies.all())
        self.assertIn(landmarks, bite.dependencies.all())

    def test_idempotent_when_jobs_already_exist(self, _send_task):
        from common.models import Job
        from common.uploads import ensure_step_jobs_for_patient

        self._ios_pipeline()
        patient = _patient()
        Job.objects.create(
            modality_slug="ios", status="completed", patient=patient, domain="maxillo"
        )

        first = ensure_step_jobs_for_patient(patient, ["ios-landmarks"])
        second = ensure_step_jobs_for_patient(patient, ["ios-landmarks"])
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(
            Job.objects.filter(patient=patient, modality_slug="ios-landmarks").count(),
            1,
        )

    def test_unknown_slugs_are_ignored(self, _send_task):
        from common.uploads import ensure_step_jobs_for_patient

        self._ios_pipeline()
        patient = _patient()
        self.assertEqual(ensure_step_jobs_for_patient(patient, ["does-not-exist"]), [])


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
        from common.uploads import create_step_jobs

        self._ios_bite()
        patient = _patient()
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
        from common.uploads import create_step_jobs

        _modality("ios")  # no steps declared
        patient = _patient()
        ios_job = Job.objects.create(
            modality_slug="ios", status="processing", patient=patient, domain="maxillo"
        )
        self.assertEqual(create_step_jobs(ios_job), [])

    @patch("common.signals.celery_app.send_task")
    def test_dependency_output_flows_into_dependent_input(self, _send):
        """The core new capability: a completed prerequisite's output_files
        become the dependent step's input_files when it unblocks."""
        from common.models import Job
        from common.uploads import create_step_jobs

        self._ios_bite()
        patient = _patient()
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
        patient = _patient()
        Job.objects.create(modality_slug=slug, status=status, patient=patient, domain="maxillo")
        return patient

    def _brain_patient_with_job(self, slug, status="pending"):
        from common.models import Job
        patient = _brain_patient()
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
