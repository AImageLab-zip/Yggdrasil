"""The structuring endpoint: who may run it, when, and what it is forbidden to touch.

The test that matters most is ``test_the_dictation_is_never_overwritten``. Everything
else here is a permission ladder or a status code; that one is the promise the whole
design was shaped around, and the only way to notice it breaking is to assert it.

No network: ``common.llm.stream_chat`` is patched. The permission tests do not even get
that far.
"""

import asyncio
import json
import time
import uuid
import warnings
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from common import caption_structuring, llm, llm_tasks
from common.external_config import STRUCTURING_METHOD_SLUG
from common.models import (
    AnnotationMethod,
    CaptionReport,
    ExternalService,
    Project,
    ProjectAccess,
    PromptTemplate,
    ReportTemplate,
    ReportTemplateField,
)
from django.apps import apps

# Through the registry, not an import: common/ may not import a domain app, tests
# included (lint-imports, pyproject.toml).
Patient = apps.get_model("maxillo", "Patient")
VoiceCaption = apps.get_model("maxillo", "VoiceCaption")


DICTATION = (
    "The lesion is on the left side and measures about twelve millimetres, "
    "with irregular margins and no restricted diffusion."
)


def consume(response):
    """The whole body of a streaming response, read the way the sync test client can.

    The structuring stream is an *async* iterator (see the ``stream`` docstring in
    ``common/domain_views/caption_reports.py``), so ``streaming_content`` is async too and
    cannot be joined directly. Iterating the response itself makes Django collect it with
    ``async_to_sync`` -- and warn that it is doing so, which is the expected price of the
    sync client and is silenced here rather than in every test.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="StreamingHttpResponse must consume asynchronous iterators"
        )
        return b"".join(response).decode()


def slow_stream(delay, text="## side\nOn the left."):
    """A model that thinks for ``delay`` seconds before its first word.

    ``time.sleep`` on purpose: the real call is a blocking read on a worker thread, and
    this is what that looks like from the event loop.
    """
    def _stream(service, messages, **kwargs):
        time.sleep(delay)
        yield text
        yield llm.ChatResult(text, usage={"total_tokens": 5}, model="test/model")
    return _stream


def fake_stream(text="## side\nOn the left.\n\n## size\nAbout twelve millimetres."):
    """Stand in for ``llm.stream_chat``: a couple of fragments, then the result."""
    def _stream(service, messages, **kwargs):
        half = len(text) // 2
        yield text[:half]
        yield text[half:]
        yield llm.ChatResult(text, usage={"total_tokens": 20}, model="test/model")
    return _stream


@override_settings(ALLOWED_HOSTS=["testserver"])
class StructuringMethodRegistryTests(TestCase):
    """The gate has to exist before anyone can open it.

    ``structuring_enabled_for`` asks whether the project has this method ticked, and the
    release notes tell administrators to tick it -- but for a while nothing created the
    row, so the answer was always no and the feature was unreachable by design rather
    than by choice. Migration 0061 creates it.
    """

    def test_the_method_exists_and_is_offered_in_every_domain(self):
        method = AnnotationMethod.objects.get(slug=STRUCTURING_METHOD_SLUG)
        self.assertTrue(method.is_active)
        # Blank domain: any domain with a report template may structure into it, and the
        # project admin form only offers a method whose domain is blank or its own.
        self.assertEqual(method.domain, "")

    def test_no_project_has_it_until_someone_ticks_it(self):
        """Creating the row must not turn the feature on anywhere."""
        method = AnnotationMethod.objects.get(slug=STRUCTURING_METHOD_SLUG)
        self.assertFalse(method.projects.exists())


class StructuringEndpointTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Maxillo Test", slug="maxillo-test", domain="maxillo"
        )
        self.method, _ = AnnotationMethod.objects.get_or_create(
            slug="voice_caption", defaults={"name": "Voice caption"}
        )
        self.structuring_method, _ = AnnotationMethod.objects.get_or_create(
            slug=STRUCTURING_METHOD_SLUG, defaults={"name": "Report structuring"}
        )
        self.project.annotation_methods.add(self.method, self.structuring_method)

        self.user = User.objects.create_user(username="clinician", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.other = User.objects.create_user(username="colleague", password="x")
        ProjectAccess.objects.create(user=self.other, project=self.project, role="admin")
        # An annotator may write annotations but is not a project admin, so
        # user_can_edit_caption refuses them someone else's caption.
        self.annotator = User.objects.create_user(username="annotator", password="x")
        ProjectAccess.objects.create(
            user=self.annotator, project=self.project, role="annotator"
        )

        self.patient = Patient.objects.create(name="P", project=self.project)
        self.caption = VoiceCaption.objects.create(
            patient=self.patient, user=self.user, duration=0.0,
            modality="cbct", text_caption=DICTATION, processing_status="completed",
        )

        template = ReportTemplate.objects.create(
            domain="maxillo", modality_slug="", name="Maxillo report"
        )
        for order, key in enumerate(("side", "size")):
            ReportTemplateField.objects.create(
                template=template, key=key, order=order, label_en=key.title()
            )

        PromptTemplate.objects.create(
            slug="caption_to_template", name="Caption to template",
            system_prompt="File dictations.",
            user_template="{fields}\n---\n{caption}",
        )
        self.service_row = ExternalService.objects.create(
            slug="openrouter_llm", name="LLM",
            kind="llm_chat_openai", base_url="https://openrouter.ai/api/v1",
            api_key_env="TEST_LLM_KEY", model_name="test/model",
        )
        self.client.force_login(self.user)

    def url(self, name="structure_caption", caption=None):
        return reverse(
            f"maxillo:{name}",
            kwargs={
                "patient_id": self.patient.patient_id,
                "caption_id": (caption or self.caption).id,
            },
        )

    def post(self, body=None, stream=None, **kwargs):
        """POST and fully consume the response before the patches come off.

        The consuming is the point. ``StreamingHttpResponse`` is lazy: its generator --
        and therefore the whole structuring run -- executes when ``streaming_content`` is
        read, not when ``post()`` returns. Reading it after the ``mock.patch`` block had
        exited ran the *real* ``llm.stream_chat`` against the network, so the first draft
        of these tests asserted on failures instead of on the feature.
        """
        with mock.patch.dict("os.environ", {"TEST_LLM_KEY": "k"}), \
                mock.patch(
                    "common.caption_structuring.llm.stream_chat",
                    stream if stream is not None else fake_stream(),
                ):
            response = self.client.post(
                self.url(), data=json.dumps(body or {}),
                content_type="application/json", **kwargs
            )
            if getattr(response, "streaming", False):
                response.body = consume(response)
            return response

    def events(self, response):
        out = []
        for line in getattr(response, "body", "").split("\n"):
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
        return out

    # --- the promise -----------------------------------------------------

    def test_the_dictation_is_never_overwritten(self):
        """The raw caption is byte-identical before and after a run, and after a rerun.

        This is the reason the structured output is its own model rather than columns on
        VoiceCaption. If this ever fails, a clinician's own words have been replaced by a
        language model's reading of them.
        """
        before = VoiceCaption.objects.get(pk=self.caption.pk).text_caption

        self.post()
        self.post()

        after = VoiceCaption.objects.get(pk=self.caption.pk)
        self.assertEqual(after.text_caption, before)
        self.assertEqual(after.original_text_caption, self.caption.original_text_caption)
        self.assertFalse(after.is_edited)
        self.assertEqual(after.edit_history, self.caption.edit_history)

    def test_a_rerun_is_a_new_attempt_that_leaves_the_first_intact(self):
        self.post()
        first = CaptionReport.objects.get()
        self.post(body={"generation_uuid": str(uuid.uuid4())})

        reports = CaptionReport.objects.order_by("attempt")
        self.assertEqual([r.attempt for r in reports], [1, 2])
        first.refresh_from_db()
        self.assertEqual(first.status, "completed")

    # --- the happy path --------------------------------------------------

    def test_it_streams_start_deltas_and_done(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        # nginx buffers proxied responses by default; without this the "stream" arrives
        # all at once when the request finishes.
        self.assertEqual(response["X-Accel-Buffering"], "no")

        events = self.events(response)
        self.assertEqual(events[0]["type"], "start")
        self.assertTrue(any(e["type"] == "delta" for e in events))
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["report"]["status"], "completed")

    def test_the_stored_report_holds_the_parsed_sections(self):
        self.post()
        report = CaptionReport.objects.get()
        self.assertEqual(set(report.structured), {"side", "size"})
        self.assertEqual(report.domain, "maxillo")
        self.assertEqual(report.get_voice_caption(), self.caption)
        self.assertEqual(report.usage["total_tokens"], 20)

    def test_the_template_is_snapshotted_against_later_edits(self):
        """A template edited afterwards must not change what an old report was read against."""
        self.post()
        ReportTemplateField.objects.all().delete()

        report = CaptionReport.objects.get()
        self.assertEqual([f["key"] for f in report.template_snapshot], ["side", "size"])

    def test_the_report_language_follows_the_users_preference(self):
        from common.models import UserPreference

        UserPreference.objects.update_or_create(
            user=self.user, defaults={"report_language": "de"}
        )
        self.post()
        self.assertEqual(CaptionReport.objects.get().report_language, "de")

    def test_the_get_endpoint_returns_the_latest_report(self):
        self.post()
        response = self.client.get(self.url("caption_report"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["report"]["status"], "completed")

    def test_the_get_endpoint_is_empty_before_any_run(self):
        response = self.client.get(self.url("caption_report"))
        self.assertIsNone(response.json()["report"])

    # --- idempotency and single flight -----------------------------------

    def test_a_replayed_generation_uuid_does_not_call_the_model_twice(self):
        key = str(uuid.uuid4())
        stream = mock.Mock(side_effect=fake_stream())
        self.post(body={"generation_uuid": key}, stream=stream)
        response = self.post(body={"generation_uuid": key}, stream=stream)

        self.assertEqual(stream.call_count, 1)
        events = self.events(response)
        self.assertTrue(events[0].get("replayed"))
        self.assertEqual(CaptionReport.objects.count(), 1)

    def test_a_second_run_while_one_is_in_flight_is_refused(self):
        CaptionReport.objects.create(
            domain="maxillo", voice_caption=self.caption, status="processing",
            started_at=__import__("django.utils.timezone", fromlist=["timezone"]).now(),
            source_text=DICTATION,
        )
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "in_flight")

    def test_a_stale_processing_row_does_not_block_for_ever(self):
        """A worker killed mid-call must not disable the button permanently."""
        from datetime import timedelta

        from django.utils import timezone

        CaptionReport.objects.create(
            domain="maxillo", voice_caption=self.caption, status="processing",
            started_at=timezone.now() - timedelta(hours=2), source_text=DICTATION,
        )
        response = self.post()
        self.assertEqual(response.status_code, 200)

    # --- the permission ladder -------------------------------------------

    def test_an_anonymous_request_is_redirected_to_login(self):
        self.client.logout()
        response = self.post()
        self.assertIn(response.status_code, (302, 403))

    def test_an_annotator_may_not_structure_someone_elses_caption(self):
        """Structuring re-presents a colleague's clinical words.

        It follows the same rule as editing the transcription: the caption's owner, or a
        project admin. An annotator with write access to the project is neither.
        """
        self.client.force_login(self.annotator)
        with mock.patch.dict("os.environ", {"TEST_LLM_KEY": "k"}):
            response = self.client.post(
                self.url(), data="{}", content_type="application/json"
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "permission_denied")

    def test_a_project_admin_may_structure_a_colleagues_caption(self):
        """The other half of the same rule, so the refusal above is not over-broad."""
        self.client.force_login(self.other)
        self.assertEqual(self.post().status_code, 200)

    def test_a_project_with_captions_disabled_refuses(self):
        self.project.annotation_methods.remove(self.method)
        response = self.post()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "disabled")

    def test_a_project_without_the_structuring_method_refuses(self):
        self.project.annotation_methods.remove(self.structuring_method)
        response = self.post()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "disabled")

    def test_the_button_and_the_refusal_agree(self):
        """One function answers both questions, so they cannot drift (commit 1de0b57)."""
        from common.structuring_context import structuring_context

        with mock.patch.dict("os.environ", {"TEST_LLM_KEY": "k"}):
            self.assertTrue(structuring_context(self.patient)["structuring_available"])

        self.project.annotation_methods.remove(self.structuring_method)
        with mock.patch.dict("os.environ", {"TEST_LLM_KEY": "k"}):
            self.assertFalse(structuring_context(self.patient)["structuring_available"])
        self.assertEqual(self.post().status_code, 403)

    def test_a_patient_with_no_project_is_not_opted_in_by_accident(self):
        """``project_allows_annotation`` returns True when there is no project.

        That is deliberate there -- legacy rows keep working -- and wrong here: a patient
        nobody assigned to a project would be opted in to sending clinical text to a
        third-party API. Asserted against the helper rather than the endpoint because
        ``maxillo.Patient.project`` is NOT NULL, so the state is unreachable through it.
        """
        from common.external_config import structuring_enabled_for
        from common.permissions import project_allows_annotation

        class Orphan:
            project = None

        self.assertTrue(project_allows_annotation(Orphan(), "voice_caption"))
        self.assertFalse(structuring_enabled_for(Orphan()))

    # --- preconditions ----------------------------------------------------

    def test_a_caption_that_is_too_short_is_refused(self):
        self.caption.text_caption = "short"
        self.caption.save()
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "too_short")

    def test_a_modality_with_no_template_is_refused(self):
        ReportTemplate.objects.all().delete()
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_template")

    def test_an_unconfigured_model_refuses_cleanly(self):
        """No key in the environment: a 503, not a traceback."""
        response = self.client.post(
            self.url(), data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "not_configured")

    def test_a_disabled_service_row_refuses_even_with_a_key_present(self):
        self.service_row.is_enabled = False
        self.service_row.save()
        response = self.post()
        self.assertEqual(response.status_code, 503)

    def test_a_missing_prompt_refuses_cleanly(self):
        PromptTemplate.objects.all().delete()
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "not_configured")

    # --- failures during the stream ---------------------------------------

    def test_an_upstream_failure_becomes_an_in_band_error_event(self):
        """The headers are already sent, so a 502 is no longer available."""
        def boom(service, messages, **kwargs):
            raise llm.LlmTimeout("too slow")
            yield  # pragma: no cover - generator marker

        response = self.post(stream=boom)

        events = self.events(response)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["code"], "upstream_timeout")
        self.assertEqual(CaptionReport.objects.get().status, "failed")

    def test_a_rate_limit_is_reported_as_such(self):
        def limited(service, messages, **kwargs):
            raise llm.LlmUpstreamError("429", status=429)
            yield  # pragma: no cover - generator marker

        events = self.events(self.post(stream=limited))
        self.assertEqual(events[-1]["code"], "rate_limited")

    def test_a_failed_run_still_leaves_the_dictation_alone(self):
        def boom(service, messages, **kwargs):
            raise llm.LlmUpstreamError("nope", status=500)
            yield  # pragma: no cover - generator marker

        self.post(stream=boom)
        self.assertEqual(
            VoiceCaption.objects.get(pk=self.caption.pk).text_caption, DICTATION
        )


@override_settings(ALLOWED_HOSTS=["testserver"])
class FinishedCaptionsOnlyTests(StructuringEndpointTests):
    """A report is filed from a whole dictation, never from part of one.

    Runs the parent's fixtures; the parent's own tests are not repeated here because
    ``StructuringEndpointTests`` is excluded below.
    """

    def test_a_caption_still_processing_is_refused(self):
        self.caption.processing_status = "processing"
        self.caption.save(update_fields=["processing_status"])
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "not_complete")
        self.assertEqual(CaptionReport.objects.count(), 0)

    def test_a_caption_whose_transcription_failed_is_refused(self):
        self.caption.processing_status = "failed"
        self.caption.save(update_fields=["processing_status"])
        response = self.post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "not_complete")


class StructurableCaptionsTests(StructuringEndpointTests):
    """What the patient list's rerun dialog is told it may structure."""

    def list_url(self):
        return reverse(
            "maxillo:structurable_captions",
            kwargs={"patient_id": self.patient.patient_id},
        )

    def get_list(self):
        with mock.patch.dict("os.environ", {"TEST_LLM_KEY": "k"}):
            return self.client.get(self.list_url())

    def test_a_finished_caption_is_offered(self):
        response = self.get_list()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [c["id"] for c in response.json()["captions"]], [self.caption.id]
        )

    def test_it_offers_exactly_what_the_endpoint_would_accept(self):
        """Each exclusion here is a refusal the Structure request would give."""
        processing = VoiceCaption.objects.create(
            patient=self.patient, user=self.user, duration=0.0, modality="cbct",
            text_caption=DICTATION, processing_status="processing",
        )
        too_short = VoiceCaption.objects.create(
            patient=self.patient, user=self.user, duration=0.0, modality="cbct",
            text_caption="short", processing_status="completed",
        )
        ids = [c["id"] for c in self.get_list().json()["captions"]]
        self.assertIn(self.caption.id, ids)
        self.assertNotIn(processing.id, ids)
        self.assertNotIn(too_short.id, ids)

    def test_someone_elses_caption_is_not_offered_to_an_annotator(self):
        self.client.force_login(self.annotator)
        self.assertEqual(self.get_list().json()["captions"], [])

    def test_attempts_so_far_are_reported(self):
        self.post()
        caption = self.get_list().json()["captions"][0]
        self.assertEqual(caption["attempts"], 1)

    def test_structuring_off_refuses_the_whole_list(self):
        self.project.annotation_methods.remove(self.structuring_method)
        response = self.get_list()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "disabled")

    def test_no_model_refuses_up_front_rather_than_once_per_caption(self):
        self.service_row.is_enabled = False
        self.service_row.save(update_fields=["is_enabled"])
        response = self.get_list()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "not_configured")


class PatientListRerunTests(StructuringEndpointTests):
    """The list row offers "Report structuring" exactly when it can do something."""

    def availability(self):
        from common.structuring_context import structuring_rerun_availability

        with mock.patch.dict("os.environ", {"TEST_LLM_KEY": "k"}):
            return structuring_rerun_availability()

    def test_a_ready_project_is_available(self):
        self.assertTrue(self.availability()(self.patient))

    def test_structuring_off_is_not_available(self):
        self.project.annotation_methods.remove(self.structuring_method)
        self.assertFalse(self.availability()(self.patient))

    def test_captions_off_is_not_available(self):
        self.project.annotation_methods.remove(self.method)
        self.assertFalse(self.availability()(self.patient))

    def test_a_domain_with_no_template_is_not_available(self):
        ReportTemplate.objects.all().delete()
        self.assertFalse(self.availability()(self.patient))

    def test_a_page_asks_each_project_once(self):
        """Fifty rows of one project cost the lookup once, not fifty times."""
        available = self.availability()
        available(self.patient)
        second = Patient.objects.create(name="Q", project=self.project)
        with self.assertNumQueries(0):
            available(second)

    def _list_page(self):
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()
        with mock.patch.dict("os.environ", {"TEST_LLM_KEY": "k"}):
            return self.client.get(reverse("maxillo:patient_list"))

    def test_the_row_carries_the_flag(self):
        response = self._list_page()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-structuring="1"')
        self.assertContains(response, "js/caption_structuring.js")

    def test_a_patient_with_no_finished_caption_is_not_flagged(self):
        self.caption.processing_status = "processing"
        self.caption.save(update_fields=["processing_status"])
        self.assertNotContains(self._list_page(), 'data-structuring="1"')


# The three classes above inherit StructuringEndpointTests for its fixtures only; without
# this, the parent's whole suite would run once per subclass.
for _cls in (FinishedCaptionsOnlyTests, StructurableCaptionsTests, PatientListRerunTests):
    for _name in [n for n in vars(StructuringEndpointTests) if n.startswith("test_")]:
        setattr(_cls, _name, None)


class UrologyModalityTests(TestCase):
    """Urology files templates under bare slugs while captions carry the prefix."""

    def test_a_prefixed_caption_modality_finds_its_template(self):
        UrologyPatient = apps.get_model("urology", "Patient")
        UrologyCaption = apps.get_model("urology", "VoiceCaption")

        project = Project.objects.create(name="Uro", slug="uro", domain="urology")
        user = User.objects.create_user(username="uro", password="x")
        ProjectAccess.objects.create(user=user, project=project, role="admin")
        patient = UrologyPatient.objects.create(name="U", project=project)
        caption = UrologyCaption.objects.create(
            patient=patient, user=user, duration=0.0,
            modality="urology-mri", text_caption=DICTATION,
            processing_status="completed",
        )
        template = ReportTemplate.objects.create(
            domain="urology", modality_slug="mri", name="Urology MRI"
        )
        ReportTemplateField.objects.create(template=template, key="side", label_en="Side")

        context = caption_structuring.build_context(
            caption, patient, user=user, task=llm_tasks.CAPTION_TO_TEMPLATE
        )
        self.assertEqual(context["template"], template)

class WarningsShownToTheClinicianTests(TestCase):
    """The panel is told that something was left out, never what it was."""

    def test_the_serialized_warnings_carry_no_word_list(self):
        report = CaptionReport(
            warnings=[
                {"code": "coverage", "detail": "About 40% ...", "missing": ["lesione"]},
                {"code": "not_filed", "detail": "Part of the dictation ..."},
            ]
        )
        serialized = caption_structuring.serialize(report)
        self.assertEqual(
            [w["code"] for w in serialized["warnings"]], ["coverage", "not_filed"]
        )
        for warning in serialized["warnings"]:
            self.assertNotIn("missing", warning)

    def test_the_row_keeps_the_diagnostic_list(self):
        """It is still there for whoever is investigating a thin-looking report."""
        report = CaptionReport(
            warnings=[{"code": "coverage", "detail": "x", "missing": ["lesione"]}]
        )
        self.assertEqual(report.warnings[0]["missing"], ["lesione"])


@override_settings(ALLOWED_HOSTS=["testserver"])
class AsgiStreamingTests(TestCase):
    """What a browser behind uvicorn actually receives, and when.

    The first version of this endpoint returned a *synchronous* generator. Under ASGI,
    Django drains one of those with ``sync_to_async(list)`` before sending anything, so
    the "stream" arrived all at once when the model finished -- measured against the
    running server, every frame landed at t = 9.05s, the ``start`` frame included. A
    model thinking for more than 30 seconds therefore tripped the browser's stall
    watchdog every time. None of the tests above could see it: the sync test client
    iterates the response synchronously, one frame at a time.

    These go through ``StreamingHttpResponse.__aiter__`` -- the path ``ASGIHandler`` uses
    -- and time each frame. Revert the view to a plain generator and the first test fails.
    """

    # The same fixture as the endpoint tests; borrowed rather than inherited so that
    # class's tests do not run twice.
    setUp = StructuringEndpointTests.setUp
    url = StructuringEndpointTests.url

    def patched(self, stream, **extra):
        patches = [
            mock.patch.dict("os.environ", {"TEST_LLM_KEY": "k"}),
            mock.patch("common.caption_structuring.llm.stream_chat", stream),
        ]
        patches += [mock.patch(target, value) for target, value in extra.items()]
        return patches

    async def open_and_read(self, stream, **extra):
        """POST through the async client and read the body as ASGI would."""
        patches = self.patched(stream, **extra)
        for patch in patches:
            patch.start()
        try:
            await self.async_client.aforce_login(self.user)
            response = await self.async_client.post(
                self.url(), data="{}", content_type="application/json"
            )
            started = time.monotonic()
            frames, buffer = [], ""
            async for chunk in response:  # StreamingHttpResponse.__aiter__
                buffer += chunk.decode()
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    frames.append((time.monotonic() - started, frame))
            return frames
        finally:
            for patch in reversed(patches):
                patch.stop()

    async def test_the_first_frame_leaves_before_the_model_answers(self):
        """The regression test for the buffering bug, with a model that takes 0.8s."""
        frames = await self.open_and_read(slow_stream(0.8))

        first_at, first = frames[0]
        self.assertIn('"type": "start"', first)
        self.assertLess(first_at, 0.4, "the start frame waited for the model: buffered")
        done_at = next(at for at, frame in frames if '"type": "done"' in frame)
        self.assertGreaterEqual(done_at, 0.8)

    async def test_silence_is_filled_with_keepalives(self):
        """A model silent for longer than the interval gets keepalives in the meantime.

        Scaled down: a 0.1s interval against a 0.55s think, standing in for 10s against
        the minutes a reasoning model can take.
        """
        frames = await self.open_and_read(
            slow_stream(0.55),
            **{"common.domain_views.caption_reports.KEEPALIVE_SECONDS": 0.1},
        )
        keepalives = [at for at, frame in frames if frame.startswith(": keepalive")]
        self.assertGreaterEqual(len(keepalives), 3)
        # Spread across the silence, not bunched at the end.
        self.assertLess(keepalives[0], 0.3)

        report = await CaptionReport.objects.aget()
        self.assertEqual(report.status, "completed")

    async def test_a_prompt_model_gets_no_keepalives(self):
        frames = await self.open_and_read(slow_stream(0.0))
        self.assertFalse([f for _at, f in frames if f.startswith(": keepalive")])
        self.assertTrue(any('"type": "done"' in f for _at, f in frames))

    async def test_a_browser_that_leaves_mid_run_frees_the_caption(self):
        """Stop, the watchdog or a closed tab must not leave the row at "processing".

        A row stuck there reads as "still working" on reload and holds the in-flight
        lock, so Run again would be refused until the lock timed out.
        """
        patches = self.patched(slow_stream(1.0))
        for patch in patches:
            patch.start()
        try:
            await self.async_client.aforce_login(self.user)
            response = await self.async_client.post(
                self.url(), data="{}", content_type="application/json"
            )
            # The body ASGIHandler iterates and closes. Django hands it back as bytes; a
            # cancellation of its pending read reaches the view's generator underneath.
            body = response._iterator
            first = await body.__anext__()
            self.assertIn(b'"type": "start"', first if isinstance(first, bytes) else first.encode())

            waiting = asyncio.ensure_future(body.__anext__())  # now blocked on the model
            await asyncio.sleep(0.2)
            waiting.cancel()  # what Django does to the response when the client leaves
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            await body.aclose()
        finally:
            for patch in reversed(patches):
                patch.stop()

        report = await CaptionReport.objects.aget()
        self.assertEqual(report.status, "failed")
        self.assertIn("Stopped before", report.error_message)

        # And the caption is free again: a second run goes through at once.
        frames = await self.open_and_read(fake_stream())
        self.assertTrue(any('"type": "done"' in frame for _at, frame in frames))
