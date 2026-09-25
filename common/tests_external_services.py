"""The external-service registry: what resolves, what refuses, and what stays secret.

Three rules are worth more than the rest, and each has its own test here:

* a **disabled** row resolves to nothing, and the environment does not rescue it --
  otherwise the tick box in the admin is a lie;
* a **missing key** resolves to nothing rather than to a half-built service that fails
  later, in front of a clinician;
* the key value itself never leaves the environment -- not into the row, not into a
  repr, not into a log line.
"""

import logging
from unittest import mock

from django.core.exceptions import ValidationError
from django.db.utils import DatabaseError
from django.test import TestCase, override_settings

from common import external_config, external_services
from common.models import ExternalService


def _whisper_row(**overrides):
    fields = {
        "slug": external_config.WHISPER_SERVICE_SLUG,
        "name": "Local Whisper",
        "kind": external_services.STT_WEBSOCKET.key,
        "base_url": "ws://whisper:9097/ws",
        "api_key_env": "TEST_WHISPER_TOKEN",
        "languages": ["it", "en"],
        "timeout_seconds": 30,
        "connect_timeout_seconds": 5,
    }
    fields.update(overrides)
    return ExternalService.objects.create(**fields)


class ResolutionPrecedenceTests(TestCase):
    """Row versus environment, stated once here and relied on everywhere else."""

    @override_settings(
        WHISPER_WS_URL="wss://external.example/ws",
        WHISPER_API_TOKEN="env-token",
        WHISPER_CA_CERT="/etc/ssl/pinned.pem",
    )
    def test_with_no_row_the_environment_is_used(self):
        """A deployment that never opens the admin behaves exactly as it did before."""
        resolved = external_config.whisper_service()
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.base_url, "wss://external.example/ws")
        self.assertEqual(resolved.api_key, "env-token")
        self.assertEqual(resolved.ca_cert, "/etc/ssl/pinned.pem")
        self.assertEqual(resolved.source, "env")

    @override_settings(
        WHISPER_WS_URL="wss://external.example/ws", WHISPER_API_TOKEN="env-token"
    )
    def test_an_enabled_row_wins_entirely(self):
        _whisper_row()
        with mock.patch.dict("os.environ", {"TEST_WHISPER_TOKEN": "row-token"}):
            resolved = external_config.whisper_service()
        self.assertEqual(resolved.base_url, "ws://whisper:9097/ws")
        self.assertEqual(resolved.api_key, "row-token")
        self.assertEqual(resolved.source, "db")

    @override_settings(
        WHISPER_WS_URL="wss://external.example/ws", WHISPER_API_TOKEN="env-token"
    )
    def test_a_disabled_row_resolves_to_nothing_even_with_the_environment_set(self):
        """Disabled means off.

        Falling back to ``settings`` here would mean an admin who unticks the box watches
        dictation keep working, and no amount of admin fiddling would stop it -- the
        switch would be decorative. The environment is a fallback for *no row*, not for a
        row that says no.
        """
        _whisper_row(is_enabled=False)
        with mock.patch.dict("os.environ", {"TEST_WHISPER_TOKEN": "row-token"}):
            self.assertIsNone(external_config.whisper_service())

    def test_an_empty_key_variable_resolves_to_nothing(self):
        _whisper_row()
        with mock.patch.dict("os.environ", {"TEST_WHISPER_TOKEN": ""}):
            self.assertIsNone(external_config.whisper_service())

    def test_a_blank_base_url_resolves_to_nothing(self):
        _whisper_row(base_url="")
        with mock.patch.dict("os.environ", {"TEST_WHISPER_TOKEN": "row-token"}):
            self.assertIsNone(external_config.whisper_service())

    def test_an_unknown_kind_resolves_to_nothing(self):
        """A row left behind by a version that declared a kind this one does not."""
        ExternalService.objects.create(
            slug="ghost", name="Ghost", kind="kind_that_no_longer_exists",
            base_url="https://example.invalid", api_key_env="TEST_WHISPER_TOKEN",
        )
        with mock.patch.dict("os.environ", {"TEST_WHISPER_TOKEN": "x"}):
            self.assertIsNone(external_config.resolve("ghost"))

    @override_settings(
        WHISPER_WS_URL="wss://external.example/ws", WHISPER_API_TOKEN="env-token"
    )
    def test_a_database_error_falls_back_to_the_environment(self):
        """A read before this table is migrated must not take dictation down with it."""
        with mock.patch(
            "common.models.ExternalService.objects.filter",
            side_effect=DatabaseError("no such table"),
        ):
            resolved = external_config.whisper_service()
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.source, "env")

    @override_settings(WHISPER_WS_URL="", WHISPER_API_TOKEN="")
    def test_nothing_configured_anywhere_resolves_to_nothing(self):
        self.assertIsNone(external_config.whisper_service())

    def test_row_parameters_are_layered_over_the_kind_defaults(self):
        _whisper_row(parameters={"beam_size": 5})
        with mock.patch.dict("os.environ", {"TEST_WHISPER_TOKEN": "t"}):
            resolved = external_config.whisper_service()
        self.assertEqual(resolved.parameter("beam_size"), 5)
        # Untouched knobs still arrive, so a caller never has to know the defaults.
        self.assertEqual(resolved.parameter("max_segment_seconds"), 12.0)


class SecrecyTests(TestCase):
    """The key is in the environment, and that is the only place it is."""

    def test_the_row_stores_a_variable_name_not_a_value(self):
        row = _whisper_row()
        self.assertEqual(row.api_key_env, "TEST_WHISPER_TOKEN")
        stored = ExternalService.objects.values().get(pk=row.pk)
        with mock.patch.dict("os.environ", {"TEST_WHISPER_TOKEN": "super-secret"}):
            self.assertNotIn("super-secret", repr(stored))

    def test_the_resolved_repr_says_whether_a_key_is_set_not_what_it_is(self):
        _whisper_row()
        with mock.patch.dict("os.environ", {"TEST_WHISPER_TOKEN": "super-secret"}):
            resolved = external_config.whisper_service()
            text = repr(resolved)
        self.assertIn("key=set", text)
        self.assertNotIn("super-secret", text)

    def test_nothing_logs_the_key_while_resolving(self):
        _whisper_row()
        with mock.patch.dict("os.environ", {"TEST_WHISPER_TOKEN": "super-secret"}):
            with self.assertLogs("common.external_config", level=logging.DEBUG) as logs:
                logging.getLogger("common.external_config").debug("probe")
                external_config.whisper_service()
        self.assertNotIn("super-secret", "\n".join(logs.output))

    def test_the_admin_reports_presence_without_the_value(self):
        row = _whisper_row()
        with mock.patch.dict("os.environ", {"TEST_WHISPER_TOKEN": "super-secret"}):
            self.assertIs(row.api_key_is_present, True)
        with mock.patch.dict("os.environ", {"TEST_WHISPER_TOKEN": ""}):
            self.assertIs(row.api_key_is_present, False)


class ResolvedServiceTests(TestCase):
    def test_a_resolved_service_cannot_be_mutated(self):
        """It is a snapshot of configuration, not a place to stash per-call state."""
        _whisper_row()
        with mock.patch.dict("os.environ", {"TEST_WHISPER_TOKEN": "t"}):
            resolved = external_config.whisper_service()
        with self.assertRaises(AttributeError):
            resolved.base_url = "ws://elsewhere/ws"


class ValidationTests(TestCase):
    """``clean()`` refuses configurations that would only fail in front of a clinician."""

    def test_a_scheme_the_kind_does_not_speak_is_refused(self):
        service = ExternalService(
            slug="bad-scheme", name="Bad", kind=external_services.STT_WEBSOCKET.key,
            base_url="https://whisper.example/ws",
        )
        with self.assertRaises(ValidationError) as caught:
            service.full_clean()
        self.assertIn("base_url", caught.exception.error_dict)

    def test_an_llm_without_a_model_is_refused(self):
        service = ExternalService(
            slug="no-model", name="No model", kind=external_services.LLM_CHAT_OPENAI.key,
            base_url="https://openrouter.ai/api/v1",
        )
        with self.assertRaises(ValidationError) as caught:
            service.full_clean()
        self.assertIn("model_name", caught.exception.error_dict)

    def test_an_unknown_parameter_is_refused_rather_than_ignored(self):
        """A typo'd knob that silently does nothing is worse than an error.

        The admin sets ``temprature``, watches the output not change, and concludes the
        field does not work.
        """
        service = ExternalService(
            slug="typo", name="Typo", kind=external_services.LLM_CHAT_OPENAI.key,
            base_url="https://openrouter.ai/api/v1", model_name="some/model",
            parameters={"temprature": 0.2},
        )
        with self.assertRaises(ValidationError) as caught:
            service.full_clean()
        self.assertIn("parameters", caught.exception.error_dict)

    def test_a_parameter_outside_its_range_is_refused(self):
        service = ExternalService(
            slug="hot", name="Hot", kind=external_services.LLM_CHAT_OPENAI.key,
            base_url="https://openrouter.ai/api/v1", model_name="some/model",
            parameters={"temperature": 7},
        )
        with self.assertRaises(ValidationError):
            service.full_clean()

    def test_a_parameter_outside_its_choices_is_refused(self):
        service = ExternalService(
            slug="odd", name="Odd", kind=external_services.STT_WEBSOCKET.key,
            base_url="ws://whisper:9097/ws", parameters={"compute_type": "float8"},
        )
        with self.assertRaises(ValidationError):
            service.full_clean()

    def test_a_valid_configuration_passes_and_coerces(self):
        service = ExternalService(
            slug="fine", name="Fine", kind=external_services.LLM_CHAT_OPENAI.key,
            base_url="https://openrouter.ai/api/v1", model_name="some/model",
            parameters={"temperature": "0.4", "max_tokens": 1500},
        )
        service.full_clean()
        self.assertEqual(service.parameters["temperature"], 0.4)

    def test_languages_must_be_a_list_of_codes(self):
        service = ExternalService(
            slug="langs", name="Langs", kind=external_services.STT_WEBSOCKET.key,
            base_url="ws://whisper:9097/ws", languages={"it": True},
        )
        with self.assertRaises(ValidationError) as caught:
            service.full_clean()
        self.assertIn("languages", caught.exception.error_dict)


class ParameterSchemaTests(TestCase):
    """The declarative half, testable without the database."""

    def test_a_boolean_is_not_accepted_as_a_number(self):
        parameter = external_services.Parameter("n", "N", "int")
        with self.assertRaises(ValueError):
            parameter.validate(True)

    def test_bounds_name_the_parameter_they_reject(self):
        parameter = external_services.Parameter("temperature", "T", "float", maximum=2.0)
        with self.assertRaises(ValueError) as caught:
            parameter.validate(5)
        self.assertIn("temperature", str(caught.exception))

    def test_kind_choices_is_a_callable_so_a_new_kind_is_not_a_migration(self):
        self.assertTrue(callable(external_services.kind_choices))
        keys = [key for key, _label in external_services.kind_choices()]
        self.assertIn(external_services.STT_WEBSOCKET.key, keys)
        self.assertIn(external_services.LLM_CHAT_OPENAI.key, keys)

    def test_an_unknown_kind_is_none_rather_than_an_exception(self):
        """A row naming a removed kind must still load in the admin to be fixed."""
        self.assertIsNone(external_services.kind_for("gone"))


class SeedCommandTests(TestCase):
    def test_seeding_is_idempotent_and_does_not_overwrite_admin_edits(self):
        from django.core.management import call_command
        from io import StringIO

        call_command("seed_external_services", stdout=StringIO())
        service = ExternalService.objects.get(slug=external_config.WHISPER_SERVICE_SLUG)
        service.base_url = "ws://my-own-whisper:9000/ws"
        service.save()

        call_command("seed_external_services", stdout=StringIO())
        service.refresh_from_db()
        self.assertEqual(service.base_url, "ws://my-own-whisper:9000/ws")

        call_command("seed_external_services", "--force", stdout=StringIO())
        service.refresh_from_db()
        self.assertEqual(service.base_url, "wss://155.185.48.254:9097/ws")

    def test_seeded_rows_are_valid_against_their_own_schema(self):
        """The shipped defaults must pass the validation the admin applies."""
        from django.core.management import call_command
        from io import StringIO

        call_command("seed_external_services", stdout=StringIO())
        for service in ExternalService.objects.all():
            service.full_clean()
