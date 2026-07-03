from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from common.job_routing import is_runner_enabled_for_modality, select_runner_queue


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
class SelectRunnerQueueTests(SimpleTestCase):
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


@override_settings(RUNNER_QUEUE_BY_MODALITY=None)
class RunnerEnabledForModalityTests(SimpleTestCase):
    def test_enabled_when_no_map_configured(self):
        self.assertTrue(is_runner_enabled_for_modality("demo"))

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"other": "q"})
    def test_disabled_when_slug_absent_from_map(self):
        self.assertFalse(is_runner_enabled_for_modality("demo"))

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"demo": "q"})
    def test_enabled_when_slug_in_map(self):
        self.assertTrue(is_runner_enabled_for_modality("demo"))

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"demo": "  "})
    def test_disabled_when_mapped_queue_is_blank(self):
        self.assertFalse(is_runner_enabled_for_modality("demo"))

    @override_settings(RUNNER_QUEUE_BY_MODALITY={"demo": "q"})
    def test_disabled_for_empty_slug_when_map_configured(self):
        self.assertFalse(is_runner_enabled_for_modality(""))
        self.assertFalse(is_runner_enabled_for_modality(None))
