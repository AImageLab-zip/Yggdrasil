"""Turning a dictated caption into a structured report.

The service layer between the view (``common/domain_views/caption_reports.py``) and the
pieces it orchestrates: the template (``common/report_templates.py``), the prompt and the
task (``common/llm_tasks.py``), the endpoint (``common/external_config.py``) and the
transport (``common/llm.py``).

Two things it guarantees, and they are the reason it exists as its own module:

* **The caption is never written to.** Nothing here imports a write to ``text_caption``;
  the output is a ``CaptionReport`` row beside the dictation.
* **The row exists before the call.** ``status='processing'`` is committed first, so a
  browser that navigates away mid-stream can still find the result afterwards -- and a
  second click finds the run already in flight instead of paying for it twice.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from common import llm, llm_tasks, report_templates
from common.external_config import llm_service, structuring_enabled_for
from common.models import CaptionReport, PromptTemplate

logger = logging.getLogger(__name__)

#: Below this a caption is a slip, not a finding. Same threshold the text-caption
#: endpoint applies (``common/domain_views/voice_captions.py``).
MIN_CAPTION_LENGTH = 10

#: A caption longer than this is not a dictation, and sending it costs tokens to no
#: purpose. Generous: a thorough multi-modality report is a few thousand characters.
MAX_CAPTION_LENGTH = 20000


class StructuringRefused(Exception):
    """A structuring request that must not proceed. ``code`` is for the browser."""

    def __init__(self, code, message, *, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def caption_text(voice_caption):
    return (getattr(voice_caption, "text_caption", "") or "").strip()


def latest_report(voice_caption, task_slug=llm_tasks.CAPTION_TO_TEMPLATE.slug):
    """The most recent attempt for this caption, or ``None``."""
    return (
        _reports_for(voice_caption)
        .filter(task_slug=task_slug)
        .order_by("-created_at", "-id")
        .first()
    )


def _reports_for(voice_caption):
    from common.domains import fk_fields_for

    domain = voice_caption._meta.app_label
    _patient_fk, voice_fk = fk_fields_for(domain)
    return CaptionReport.objects.filter(domain=domain, **{voice_fk: voice_caption})


def in_flight_report(voice_caption, service):
    """A run that is still going, if there is one.

    "Still going" is bounded by the service's own timeout plus a minute: a worker killed
    mid-call leaves a row at ``processing`` for ever, and without the bound one crash
    would disable the button permanently with no way to clear it from the UI.
    """
    timeout = getattr(service, "timeout", 60) or 60
    cutoff = timezone.now() - timedelta(seconds=timeout + 60)
    return (
        _reports_for(voice_caption)
        .filter(status="processing", started_at__gte=cutoff)
        .order_by("-created_at")
        .first()
    )


def prompt_for(task):
    prompt = PromptTemplate.objects.filter(slug=task.prompt_slug, is_active=True).first()
    if prompt is None:
        raise StructuringRefused(
            "not_configured",
            "No prompt is configured for this task. Run manage.py seed_llm_prompts.",
            status=503,
        )
    return prompt


def build_context(voice_caption, patient, *, user, task, language=None):
    """Everything the task needs, resolved. Raises ``StructuringRefused`` if it cannot be."""
    text = caption_text(voice_caption)
    if len(text) < MIN_CAPTION_LENGTH:
        raise StructuringRefused(
            "too_short",
            "This caption is too short to structure. Finish dictating first.",
            status=409,
        )
    if len(text) > MAX_CAPTION_LENGTH:
        raise StructuringRefused(
            "too_long", "This caption is too long to structure.", status=409
        )

    domain = voice_caption._meta.app_label
    project = getattr(patient, "project", None)
    template = report_templates.template_for(
        domain, getattr(voice_caption, "modality", ""), project
    )
    if template is None:
        raise StructuringRefused(
            "no_template",
            "No report template is defined for this modality.",
            status=409,
        )

    report_language = report_templates.normalize_language(
        language or _user_report_language(user)
    )
    fields = report_templates.field_records(template, report_language)
    if not fields:
        raise StructuringRefused(
            "no_template", "The report template for this modality has no fields.",
            status=409,
        )

    return {
        "caption": text,
        "fields": fields,
        "template": template,
        "report_language": report_language,
        "source_language": _source_language(voice_caption),
        "task": task,
    }


def _user_report_language(user):
    from common.models import UserPreference

    if user is None or not getattr(user, "is_authenticated", False):
        return report_templates.DEFAULT_REPORT_LANGUAGE
    preference = UserPreference.objects.filter(user=user).first()
    return preference.report_language if preference else report_templates.DEFAULT_REPORT_LANGUAGE


def _source_language(voice_caption):
    """What it was dictated in, if the caption recorded it.

    The speech language is chosen in the browser and, historically, thrown away at save
    time. Captions saved before that was fixed have nothing here, and the prompt then
    tells the model to infer it rather than asserting a language that may be wrong.
    """
    return (getattr(voice_caption, "speech_language", "") or "").strip()


def ensure_allowed(patient):
    """The per-project switch. Raises ``StructuringRefused`` when it is off."""
    if not structuring_enabled_for(patient):
        raise StructuringRefused(
            "disabled",
            "Report structuring is not enabled for this project.",
            status=403,
        )


def start_report(voice_caption, patient, *, user, task, context, generation_uuid=None):
    """Create the ``processing`` row, before anything is sent anywhere."""
    service = llm_service(task.service_slug)
    if service is None:
        raise StructuringRefused(
            "not_configured",
            "No language model is configured on this server.",
            status=503,
        )

    existing = in_flight_report(voice_caption, service)
    if existing is not None:
        raise StructuringRefused(
            "in_flight",
            "This caption is already being structured.",
            status=409,
        )

    prompt = prompt_for(task)
    template = context["template"]
    field_keys = [field["key"] for field in context["fields"]]
    previous = latest_report(voice_caption, task.slug)

    from common.models import ExternalService

    report = CaptionReport(
        task_slug=task.slug,
        generation_uuid=generation_uuid,
        attempt=(previous.attempt + 1) if previous else 1,
        template=template,
        template_snapshot=context["fields"],
        source_text=context["caption"],
        source_fingerprint=llm_tasks.fingerprint(
            context["caption"], field_keys, prompt.version
        ),
        source_language=context["source_language"],
        report_language=context["report_language"],
        status="processing",
        started_at=timezone.now(),
        service=ExternalService.objects.filter(slug=service.slug).first(),
        model_name=service.model_name,
        prompt_template=prompt,
        prompt_version=prompt.version,
        requested_by=user if getattr(user, "is_authenticated", False) else None,
    )
    report.set_voice_caption(voice_caption)
    report.save()
    return report, service, prompt


def replay(voice_caption, generation_uuid, task_slug):
    """A completed report for this idempotency key, if one exists.

    The ``save_browser_panoramic`` precedent: a retried fetch, a double click or a
    back-button replay must not buy a second model call.
    """
    if not generation_uuid:
        return None
    return (
        _reports_for(voice_caption)
        .filter(generation_uuid=generation_uuid, task_slug=task_slug, status="completed")
        .order_by("-created_at")
        .first()
    )


def run(report, service, prompt, context):
    """Do the call, yielding each fragment as it arrives, then record the outcome.

    A generator, not a function with a callback: the view has to *yield* each piece to
    the browser as it lands, and a callback cannot yield out of the generator that called
    it. Collecting the fragments and emitting them afterwards would produce a response
    that looks streamed and is not -- the clinician would still wait for the whole answer
    before seeing the first word.

    The report is completed from the accumulated text here rather than in the view, so a
    browser that disconnects mid-stream still leaves a finished row behind.
    """
    task = context["task"]
    messages, rendered_user = task.build_messages(prompt, context)
    report.prompt_rendered = rendered_user
    report.save(update_fields=["prompt_rendered", "updated_at"])

    try:
        result = None
        for item in llm.stream_chat(service, messages):
            if isinstance(item, llm.ChatResult):
                result = item
                break
            yield item
        if result is None:
            raise llm.LlmMalformedResponse("The stream ended without a result")
    except llm.LlmError as exc:
        report.mark_failed(str(exc))
        raise

    structured, rendered, warnings = task.parse(result.text, context)
    report.mark_completed(
        structured,
        rendered,
        warnings=warnings,
        usage=result.usage,
        model_name=result.model,
    )


def _warnings_for_display(warnings):
    """The warnings minus their diagnostic payload.

    ``coverage`` carries the list of tokens it could not find, which is useful when
    someone is investigating why a report looks thin -- and is exactly what should not
    be put in front of a clinician, who would be reading fragments of a dictation
    alongside a report that leaves them out. The list stays on the row.
    """
    display = []
    for warning in warnings or []:
        if isinstance(warning, dict):
            display.append({
                key: value for key, value in warning.items() if key != "missing"
            })
        else:  # pragma: no cover - defensive, warnings are always dicts
            display.append(warning)
    return display


def serialize(report):
    """The shape the browser reads, for both the stream's last frame and the GET."""
    if report is None:
        return None
    return {
        "id": report.id,
        "attempt": report.attempt,
        "status": report.status,
        "structured": report.structured,
        "structured_text": report.structured_text,
        "warnings": _warnings_for_display(report.warnings),
        "error": report.error_message,
        "template": report.template.name if report.template_id else "",
        "report_language": report.report_language,
        "source_language": report.source_language,
        "model": report.model_name,
        "created_at": report.created_at.isoformat() if report.created_at else "",
        "completed_at": report.completed_at.isoformat() if report.completed_at else "",
        "requested_by": (
            report.requested_by.get_full_name() or report.requested_by.username
            if report.requested_by_id else ""
        ),
    }
