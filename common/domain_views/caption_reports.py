"""Structuring a dictated caption into the report template.

Two endpoints, the same in every domain (``common/domain_models.py`` resolves the models,
so no domain app is imported here):

* ``POST .../structure/`` runs the model and **streams** the answer back as
  server-sent events, so the clinician watches the report fill in rather than a spinner;
* ``GET  .../report/`` returns the latest stored report, which is what a page reload and
  a dropped connection both rely on.

Why SSE rather than a plain POST: the wait is 10-40 seconds on a free-tier model, and
this platform already trains clinicians to watch text appear while they dictate. Why not
``EventSource``: it is GET-only, so the caption id would ride in URLs and access logs, it
cannot send a CSRF token, and it reconnects automatically on close -- which would silently
buy a second model call.

The row is written before the call, so nothing here depends on the browser staying
connected: an abandoned tab still finds its report on the next GET.
"""

import json
import logging
import uuid

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from common import caption_structuring, llm, llm_tasks
from common.caption_structuring import StructuringRefused
from common.domain_models import get_domain_models
from common.permissions import project_allows_annotation, user_can_edit_caption

logger = logging.getLogger(__name__)


def _sse(event):
    return f"data: {json.dumps(event)}\n\n"


def _streaming_response(generator):
    response = StreamingHttpResponse(generator, content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    # nginx buffers proxied responses by default and nginx.conf does not turn it off, so
    # without this the whole "stream" arrives at once when the request finishes -- which
    # looks exactly like the feature not working.
    response["X-Accel-Buffering"] = "no"
    return response


def _resolve(request, patient_id, caption_id):
    """The patient and caption, after every check. Raises ``StructuringRefused``."""
    domain_models = get_domain_models(request)
    patient = get_object_or_404(domain_models["Patient"], patient_id=patient_id)
    voice_caption = get_object_or_404(
        domain_models["VoiceCaption"], id=caption_id, patient=patient
    )

    # Same rule as editing the transcription: structuring re-presents a colleague's
    # clinical words, so it belongs to whoever may already rewrite them.
    if not user_can_edit_caption(request.user, voice_caption):
        raise StructuringRefused(
            "permission_denied",
            "You do not have permission to structure this caption.",
            status=403,
        )
    # Captions off means structuring off; the narrower switch is checked after it.
    if not project_allows_annotation(patient, "voice_caption"):
        raise StructuringRefused(
            "disabled", "Voice captions are disabled for this project.", status=403
        )
    caption_structuring.ensure_allowed(patient)
    return patient, voice_caption


@login_required
@require_POST
def structure_caption(request, patient_id, caption_id):
    """Run the model over this caption and stream the report back."""
    task = llm_tasks.get_task(
        (request.POST.get("task") if request.POST else None)
        or llm_tasks.CAPTION_TO_TEMPLATE.slug
    )
    if task is None:
        return JsonResponse({"error": "Unknown task", "code": "unknown_task"}, status=400)

    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}

    generation_uuid = body.get("generation_uuid") or None
    if generation_uuid:
        try:
            generation_uuid = uuid.UUID(str(generation_uuid))
        except (ValueError, AttributeError, TypeError):
            generation_uuid = None

    try:
        patient, voice_caption = _resolve(request, patient_id, caption_id)

        replayed = caption_structuring.replay(voice_caption, generation_uuid, task.slug)
        if replayed is not None:
            # A retried request, not a second opinion: hand back what that key already
            # produced rather than paying for the same answer twice.
            return _streaming_response(iter([
                _sse({"type": "start", "caption_id": voice_caption.id,
                      "replayed": True, "model": replayed.model_name}),
                _sse({"type": "done",
                      "report": caption_structuring.serialize(replayed)}),
            ]))

        context = caption_structuring.build_context(
            voice_caption, patient, user=request.user, task=task,
            language=body.get("language"),
        )
        report, service, prompt = caption_structuring.start_report(
            voice_caption, patient, user=request.user, task=task, context=context,
            generation_uuid=generation_uuid,
        )
    except StructuringRefused as refusal:
        # Before the stream opens, an ordinary JSON error is far easier for the browser
        # to act on than an in-band SSE event.
        return JsonResponse(
            {"error": refusal.message, "code": refusal.code}, status=refusal.status
        )

    def stream():
        yield _sse({
            "type": "start",
            "caption_id": voice_caption.id,
            "report_id": report.id,
            "attempt": report.attempt,
            "model": report.model_name,
            "template": report.template.name if report.template_id else "",
            # The headings the browser should show while the answer streams in. The model
            # writes section *keys*; without these the clinician watches "## pi_rads_score"
            # appear and then be replaced by the finished report a moment later.
            "sections": task.section_labels(context),
            "omit": list(task.omit_sections),
        })
        try:
            for fragment in caption_structuring.run(report, service, prompt, context):
                yield _sse({"type": "delta", "text": fragment})
        except llm.LlmError as exc:
            logger.warning("Structuring caption %s failed: %s", voice_caption.id, exc)
            yield _sse({"type": "error", "code": _error_code(exc), "detail": str(exc)})
            return
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Structuring caption %s crashed", voice_caption.id)
            report.mark_failed(str(exc))
            yield _sse({"type": "error", "code": "failed", "detail": "Structuring failed."})
            return

        # "done" carries the parsed, stored report, so the browser never has to trust its
        # own concatenation of the fragments above.
        report.refresh_from_db()
        yield _sse({"type": "done", "report": caption_structuring.serialize(report)})

    return _streaming_response(stream())


def _error_code(exc):
    if isinstance(exc, llm.LlmTimeout):
        return "upstream_timeout"
    if isinstance(exc, llm.LlmUnavailable):
        return "not_configured"
    if isinstance(exc, llm.LlmUpstreamError):
        return "rate_limited" if getattr(exc, "status", None) == 429 else "upstream_error"
    if isinstance(exc, llm.LlmBudgetExhausted):
        return "budget_exhausted"
    if isinstance(exc, llm.LlmMalformedResponse):
        return "malformed_response"
    return "failed"


@login_required
@require_GET
def caption_report(request, patient_id, caption_id):
    """The latest stored report for this caption.

    What a page reload reads, and what a browser that lost the stream falls back to.
    """
    try:
        _patient, voice_caption = _resolve(request, patient_id, caption_id)
    except StructuringRefused as refusal:
        return JsonResponse(
            {"error": refusal.message, "code": refusal.code}, status=refusal.status
        )

    report = caption_structuring.latest_report(voice_caption)
    return JsonResponse({
        "caption_id": voice_caption.id,
        "report": caption_structuring.serialize(report),
    })
