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

import asyncio
import json
import logging
import threading
import uuid

from asgiref.sync import sync_to_async
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from common import caption_structuring, llm, llm_tasks
from common.caption_structuring import StructuringRefused
from common.domain_models import get_domain_models
from common.external_config import llm_service
from common.permissions import project_allows_annotation, user_can_edit_caption

logger = logging.getLogger(__name__)


def _sse(event):
    return f"data: {json.dumps(event)}\n\n"


#: Seconds of silence after which the server sends a keepalive of its own. Well inside
#: the browser's 30-second stall watchdog (``static/js/caption_structuring.js``), which
#: re-arms on any bytes and ignores comment lines. A reasoning model can think for
#: minutes before its first word; without this the browser aborted every such run.
KEEPALIVE_SECONDS = 10

#: An SSE comment: keeps the connection and the watchdog alive, and every SSE reader --
#: ours included -- skips it.
KEEPALIVE_FRAME = ": keepalive\n\n"

_KEEPALIVE = object()
_END = object()


async def _relay(service, messages, *, keepalive_seconds):
    """Run ``llm.stream_chat`` on a worker thread; yield what it yields, or ``_KEEPALIVE``.

    The model call is a blocking ``requests`` read, so it cannot run on the event loop.
    It runs on the loop's executor instead, handing each item across a queue, and the
    loop waits on that queue with a timeout -- which is what lets the server say
    something while the model is saying nothing. The keepalive therefore comes from the
    server's own clock and does not depend on the provider sending any of its own.

    The worker never touches the database, so it needs no connection of its own.
    """
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    stop = threading.Event()

    def put(item):
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            # The loop is gone: the response was torn down while the model was still
            # answering. Nobody is left to read this, so there is nothing to do.
            pass

    def pump():
        try:
            for item in llm.stream_chat(service, messages):
                put(item)
                if stop.is_set():
                    break  # closes the upstream response via stream_chat's finally
        except BaseException as exc:  # handed across and re-raised on the loop
            put(exc)
        finally:
            put(_END)

    loop.run_in_executor(None, pump)
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), keepalive_seconds)
            except asyncio.TimeoutError:
                yield _KEEPALIVE
                continue
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stop.set()


async def _once(*frames):
    """An async iterator over fixed frames.

    Under ASGI a *synchronous* iterator is drained to a list before anything is sent, so
    every response from this module is asynchronous, including the trivial ones.
    """
    for frame in frames:
        yield frame


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
            return _streaming_response(_once(
                _sse({"type": "start", "caption_id": voice_caption.id,
                      "replayed": True, "model": replayed.model_name}),
                _sse({"type": "done",
                      "report": caption_structuring.serialize(replayed)}),
            ))

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

    # Built here, on the request thread: the stream below runs on the event loop, where a
    # lazy foreign-key read such as ``report.template.name`` is a SynchronousOnlyOperation.
    start = _sse({
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

    def settle():
        """``serialize`` the stored report after a refresh -- database work, so sync."""
        report.refresh_from_db()
        return caption_structuring.serialize(report)

    async def stream():
        """The response body. **Asynchronous on purpose.**

        This used to be a plain generator, and under uvicorn Django drains a synchronous
        iterator completely -- ``sync_to_async(list)`` in ``StreamingHttpResponse`` --
        before sending a byte. Nothing streamed: the browser received every frame at once
        when the model finished, and its 30-second stall watchdog killed any run whose
        model took longer to think. The test client iterates synchronously, so no test
        ever saw it. ``tests_caption_reports.AsgiStreamingTests`` pins it now.
        """
        settled = False
        try:
            # Inside the try: a browser can leave while this very frame is in flight, and
            # the row must still be closed off rather than left at "processing".
            yield start
            messages = await sync_to_async(caption_structuring.begin)(report, prompt, context)
            result = None
            async for item in _relay(
                service, messages, keepalive_seconds=KEEPALIVE_SECONDS
            ):
                if item is _KEEPALIVE:
                    yield KEEPALIVE_FRAME
                elif isinstance(item, llm.ChatResult):
                    result = item
                else:
                    yield _sse({"type": "delta", "text": item})
            await sync_to_async(caption_structuring.finish)(report, context, result)
            settled = True
        except llm.LlmError as exc:
            logger.warning("Structuring caption %s failed: %s", voice_caption.id, exc)
            await sync_to_async(report.mark_failed)(str(exc))
            settled = True
            yield _sse({"type": "error", "code": _error_code(exc), "detail": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - reported, then shown as a failure
            logger.exception("Structuring caption %s crashed", voice_caption.id)
            await sync_to_async(report.mark_failed)(str(exc))
            settled = True
            yield _sse({"type": "error", "code": "failed", "detail": "Structuring failed."})
            return
        finally:
            if not settled:
                # The browser went away mid-run -- Stop, its watchdog, or a closed tab --
                # and the response is being torn down. Shielded because this cleanup may
                # itself be running inside a cancellation.
                await asyncio.shield(sync_to_async(caption_structuring.abandon)(report))

        # "done" carries the parsed, stored report, so the browser never has to trust its
        # own concatenation of the fragments above.
        yield _sse({"type": "done", "report": await sync_to_async(settle)()})

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


@login_required
@require_GET
def structurable_captions(request, patient_id):
    """This patient's captions that a Structure request would accept.

    What the patient list's rerun dialog asks before it walks them through
    ``structure_caption`` one at a time. Structuring is deliberately not a pipeline job
    -- there is no ``Job`` row, and nothing here touches the runner API -- so a rerun
    from the list is the same per-caption request the patient page makes, issued once
    per caption this returns.
    """
    domain_models = get_domain_models(request)
    patient = get_object_or_404(domain_models["Patient"], patient_id=patient_id)
    task = llm_tasks.CAPTION_TO_TEMPLATE
    try:
        if not project_allows_annotation(patient, "voice_caption"):
            raise StructuringRefused(
                "disabled", "Voice captions are disabled for this project.", status=403
            )
        caption_structuring.ensure_allowed(patient)
        # Refuse up front rather than let every caption fail the same way in turn.
        if llm_service(task.service_slug) is None:
            raise StructuringRefused(
                "not_configured",
                "No language model is configured on this server.",
                status=503,
            )
    except StructuringRefused as refusal:
        return JsonResponse(
            {"error": refusal.message, "code": refusal.code}, status=refusal.status
        )

    voice_captions = domain_models["VoiceCaption"].objects.filter(
        patient=patient
    ).order_by("created_at", "id")
    eligible = caption_structuring.structurable_captions(
        patient, voice_captions, user=request.user, task=task
    )
    captions = []
    for voice_caption in eligible:
        latest = caption_structuring.latest_report(voice_caption, task.slug)
        captions.append({
            "id": voice_caption.id,
            "modality": getattr(voice_caption, "modality", "") or "",
            "attempts": latest.attempt if latest else 0,
        })
    return JsonResponse({"patient_id": patient.patient_id, "captions": captions})
