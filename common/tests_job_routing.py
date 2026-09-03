from types import SimpleNamespace

from django.test import TestCase, override_settings

from common.job_routing import is_runner_enabled_for_modality, select_runner_queue
from common.models import Modality, ProcessingStep


def _job(domain="maxillo", modality_slug="demo", **attrs):
    return SimpleNamespace(domain=domain, modality_slug=modality_slug, **attrs)


# Class-level None overrides neutralize whatever a local .env put into
# settings (select_runner_queue treats None like an absent setting), so these
# tests pass identically on dev machines and in CI.
@override_settings(
    RUNNER_DEFAULT_QUEUE=None,
    RUNNER_QUEUE_BY_PROJECT=None,
    RUNNER_QUEUE_BY_MODALITY=None,
)
class SelectRunnerQueueTests(TestCase):
    def test_default_queue_without_settings(self):
        self.assertEqual(select_runner_queue(_job()), "runner")

    @override_settings(RUNNER_DEFAULT_QUEUE="gpu")
    def test_default_queue_override(self):
        self.assertEqual(select_runner_queue(_job()), "gpu")

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"demo": "modality-q"})
    def test_modality_map_beats_default(self):
        self.assertEqual(select_runner_queue(_job()), "modality-q")

    @override_settings(
        RUNNER_QUEUE_BY_PROJECT={"maxillo": "project-q"},
        RUNNER_QUEUE_BY_MODALITY={"demo": "modality-q"},
    )
    def test_project_map_beats_modality_map(self):
        job = _job(
            patient=SimpleNamespace(project=SimpleNamespace(slug="maxillo")),
        )
        self.assertEqual(select_runner_queue(job), "project-q")

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"demo": "bad queue!"})
    def test_invalid_mapped_queue_falls_back_to_default(self):
        self.assertEqual(select_runner_queue(_job()), "runner")

    @override_settings(RUNNER_DEFAULT_QUEUE="bad name!")
    def test_invalid_default_queue_falls_back_to_runner(self):
        self.assertEqual(select_runner_queue(_job()), "runner")

    @override_settings(RUNNER_QUEUE_BY_PROJECT={"brain": "brain-q"})
    def test_brain_domain_resolves_project_queue(self):
        job = _job(domain="brain", brain_patient=SimpleNamespace())
        self.assertEqual(select_runner_queue(job), "brain-q")

    @override_settings(RUNNER_QUEUE_BY_PROJECT={"laparoscopy": "lap-q"})
    def test_laparoscopy_domain_resolves_project_queue(self):
        job = _job(domain="laparoscopy", laparoscopy_patient=SimpleNamespace())
        self.assertEqual(select_runner_queue(job), "lap-q")

    @override_settings(
        RUNNER_QUEUE_BY_PROJECT={"maxillo": "project-q"},
        RUNNER_QUEUE_BY_MODALITY={"demo": "modality-q"},
    )
    def test_db_queue_override_beats_all_env(self):
        modality = Modality.objects.create(slug="demo", name="Demo")
        ProcessingStep.objects.create(modality=modality, name="demo", slug="demo", queue_name="db-q")
        job = _job(patient=SimpleNamespace(project=SimpleNamespace(slug="maxillo")))
        self.assertEqual(select_runner_queue(job), "db-q")

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"demo": "modality-q"})
    def test_blank_db_queue_falls_back_to_env(self):
        modality = Modality.objects.create(slug="demo", name="Demo")
        ProcessingStep.objects.create(modality=modality, name="demo", slug="demo", queue_name="")
        self.assertEqual(select_runner_queue(_job()), "modality-q")


@override_settings(RUNNER_QUEUE_BY_MODALITY=None)
class RunnerEnabledForModalityTests(TestCase):
    """A step declares the work; the env map only says which queue it runs on.

    RUNNER_QUEUE_BY_MODALITY used to double as an enablement switch, and its
    empty default enabled *everything* -- so an admin-added modality with no
    step got a Job the runner could only fail ("No algo_name configured for
    this step"). Enablement now comes from the step alone.
    """

    def _step(self, slug="demo", **kwargs):
        modality = Modality.objects.create(slug=slug, name=slug.upper())
        return ProcessingStep.objects.create(
            modality=modality, name=slug, slug=slug, **kwargs
        )

    def test_disabled_when_no_step_declares_the_slug(self):
        self.assertFalse(is_runner_enabled_for_modality("demo"))

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"demo": "q"})
    def test_a_queue_entry_alone_does_not_enable(self):
        self.assertFalse(is_runner_enabled_for_modality("demo"))

    def test_disabled_for_empty_slug(self):
        self.assertFalse(is_runner_enabled_for_modality(""))
        self.assertFalse(is_runner_enabled_for_modality(None))

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"other": "q"})
    def test_enabled_step_wins_over_an_env_map_without_it(self):
        self._step(is_enabled=True)
        self.assertTrue(is_runner_enabled_for_modality("demo"))

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"demo": "q"})
    def test_disabled_step_wins_over_its_env_queue_entry(self):
        self._step(is_enabled=False)
        self.assertFalse(is_runner_enabled_for_modality("demo"))
