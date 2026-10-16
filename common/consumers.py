import asyncio
import json
import logging
import ssl
from contextlib import suppress
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import websockets
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.apps import apps
from django.conf import settings

from common import assist
from common.domains import DOMAINS
from common.permissions import project_allows_annotation, user_can_write_patient_annotations


SUPPORTED_LANGUAGES = frozenset({"it", "en", "es", "fr", "de"})
MAX_AUDIO_FRAME_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


def _upstream_url(language):
    parts = urlsplit(settings.WHISPER_WS_URL)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["token"] = [settings.WHISPER_API_TOKEN]
    query["lang"] = [language]
    encoded = urlencode(query, doseq=True, quote_via=quote)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, encoded, parts.fragment))


def _ssl_context():
    if not settings.WHISPER_CA_CERT:
        raise RuntimeError("WHISPER_CA_CERT is not configured")
    return ssl.create_default_context(cafile=settings.WHISPER_CA_CERT)


@database_sync_to_async
def _can_transcribe(user, domain, patient_id):
    """Whether ``user`` may dictate into this patient's captions.

    A caption is annotation work, so this is the same check the HTTP caption endpoints
    make, through the same helper -- and deliberately not a second spelling of it.

    **The project is the patient's, not the domain's.** This used to pass the domain
    *slug* as the permission context, and a string used to resolve through
    ``entry_project_for(None, domain)``: the domain's first active project by name, for
    nobody in particular (the helpers now refuse anything but a ``Project``). Every patient in
    every other project of the domain was therefore checked against a project it is not
    in, and refused with 4403 -- live captions stopped working on a page whose Record
    button, save and every other write still did, because those resolve the project the
    normal way. A deployment with one project per domain happens to agree, which is why
    the suite passed; ``test_allows_a_patient_outside_the_domain_first_project`` is the
    case that did not.
    """
    if domain not in DOMAINS or not user or not user.is_authenticated:
        return False

    Patient = apps.get_model(domain, "Patient")
    patient = Patient.objects.filter(patient_id=patient_id).first()
    if patient is None:
        return False
    return user_can_write_patient_annotations(user, patient)


class LiveTranscriptionConsumer(AsyncWebsocketConsumer):
    upstream = None
    upstream_task = None

    async def connect(self):
        user = self.scope.get("user")
        domain = self.scope["url_route"]["kwargs"]["domain"]
        patient_id = self.scope["url_route"]["kwargs"]["patient_id"]
        language = parse_qs(self.scope.get("query_string", b"").decode()).get(
            "lang", ["it"]
        )[0]

        if not user or not user.is_authenticated:
            await self._refuse(4401, "the handshake carried no authenticated session")
            return
        if language not in SUPPORTED_LANGUAGES:
            await self._refuse(4400, f"{language!r} is not a language Whisper is asked for")
            return
        if not await _can_transcribe(user, domain, patient_id):
            await self._refuse(
                4403,
                f"{user} may not write annotations on {domain} patient {patient_id}",
            )
            return
        if not settings.WHISPER_API_TOKEN or not settings.WHISPER_WS_URL:
            await self._refuse(4503, "WHISPER_API_TOKEN or WHISPER_WS_URL is unset")
            return

        try:
            self.upstream = await websockets.connect(
                _upstream_url(language),
                ssl=_ssl_context(),
                open_timeout=settings.WHISPER_CONNECT_TIMEOUT,
                max_size=1024 * 1024,
            )
        except Exception as exc:
            logger.warning("Could not connect to Live Whisper: %s", exc)
            await self.close(code=4502)
            return

        await self.accept()
        await self.send(text_data=json.dumps({"type": "ready"}))
        self.upstream_task = asyncio.create_task(self._relay_upstream())

    async def _refuse(self, code, reason):
        """Close before accepting, and say why in the log.

        **Every one of these reaches the server log as the same line.** A close before
        ``accept`` is reported by uvicorn as a bare
        ``"WebSocket /ws/live-transcription/laparoscopy/15/?lang=it" 403``, so an expired
        session, an unsupported language, a permission refusal, a missing token and an
        unreachable Whisper are indistinguishable in it -- and "the live caption endpoint
        does not work any more" is exactly the report that needs them told apart. The
        browser is told only the code (`vocal_caption.js:transcriptionCloseMessage`),
        which is the right amount for a clinician and not enough for whoever is asked why.
        """
        logger.warning("Refusing live transcription (%s): %s", code, reason)
        await self.close(code=code)

    async def receive(self, text_data=None, bytes_data=None):
        if text_data is not None or bytes_data is None:
            await self.close(code=4400)
            return
        if len(bytes_data) > MAX_AUDIO_FRAME_BYTES:
            await self.close(code=4409)
            return
        if self.upstream is None:
            await self.close(code=4502)
            return
        try:
            await self.upstream.send(bytes_data)
        except websockets.ConnectionClosed:
            await self.close(code=4502)

    async def disconnect(self, close_code):
        current = asyncio.current_task()
        if self.upstream_task and self.upstream_task is not current:
            self.upstream_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.upstream_task
        if self.upstream is not None:
            with suppress(Exception):
                await self.upstream.close()
            self.upstream = None

    async def _relay_upstream(self):
        try:
            async for message in self.upstream:
                if not isinstance(message, str):
                    continue
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") not in {"partial", "final"}:
                    continue
                await self.send(
                    text_data=json.dumps(
                        {"type": payload["type"], "text": str(payload.get("text", ""))}
                    )
                )
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed:
            pass
        finally:
            await self.close(code=4502)


# What the browser may be told about a worker error: never the worker's own text.
_WORKER_ERRORS = {
    "session_expired": "The assistant session ended. Reload the page to start it again.",
    "load_failed": "The assistant could not load this video.",
    "inference_failed": "The assistant could not segment this frame.",
}


@database_sync_to_async
def _assist_target(user, domain, patient_id, file_id):
    """``(close_code, None)`` or ``(None, (video_link, probe))`` for the video to assist on.

    The video is found from the patient, never taken from the browser: ``fileId`` must name
    *this* patient's subsampled derivative, the file Annotation Mode's frames come from.
    The worker is sent a signed link to it (``assist.video_link``), never a storage key it
    would need credentials for.
    """
    if not user or not user.is_authenticated:
        return 4401, None
    if domain not in DOMAINS:
        return 4404, None
    Patient = apps.get_model(domain, "Patient")
    patient = Patient.objects.select_related("project").filter(patient_id=patient_id).first()
    if patient is None:
        return 4404, None
    if not user_can_write_patient_annotations(user, patient):
        return 4403, None
    if not project_allows_annotation(patient, assist.METHOD_SLUG):
        return 4403, None
    video = patient.files.filter(
        pk=file_id, file_type="video_processed", subtype="subsampled"
    ).first()
    probe = assist.probe_of(video) if video else None
    if probe is None:
        return 4404, None
    return None, (assist.video_link(video.file_path), probe)


class AssistSessionConsumer(AsyncWebsocketConsumer):
    """One page visit's assisted-segmentation session, relayed to the GPU worker.

    The browser names the patient (URL) and the video (``?fileId=``); everything the worker
    is told -- the video link, the session id, the token -- is decided here. The worker
    starts precomputing on connect; leaving the page closes this socket, and that closes the
    worker session at once instead of after its grace period.
    """

    upstream = None
    upstream_task = None
    session_id = None
    probe = None
    ready = False

    async def connect(self):
        user = self.scope.get("user")
        kwargs = self.scope["url_route"]["kwargs"]
        query = parse_qs(self.scope.get("query_string", b"").decode())
        try:
            file_id = int(query.get("fileId", [""])[0])
        except ValueError:
            await self._refuse(4400, "fileId is not an integer")
            return
        if not assist.is_configured():
            await self._refuse(4503, "ASSIST_WORKER_URL or ASSIST_WORKER_TOKEN is unset")
            return
        code, target = await _assist_target(user, kwargs["domain"], kwargs["patient_id"], file_id)
        if code:
            await self._refuse(
                code,
                f"{user} may not assist on {kwargs['domain']} patient {kwargs['patient_id']} file {file_id}",
            )
            return
        video_link, self.probe = target
        self.session_id = assist.new_session_id(file_id)
        try:
            # Subscribe before /ready: the worker's session_ready is pub/sub, and is lost if
            # nobody is listening when precompute finishes.
            self.upstream = await websockets.connect(
                assist.upstream_ws_url(self.session_id),
                additional_headers=assist.auth_headers(),
                open_timeout=settings.ASSIST_CONNECT_TIMEOUT,
                max_size=8 * 1024 * 1024,
            )
            await asyncio.to_thread(
                assist.worker_request,
                "POST",
                "/api/session/ready/",
                {"video_id": self.session_id, "video_source": video_link},
            )
        except Exception as exc:
            logger.warning("Could not open an assist session: %s", exc)
            # The socket may be open and /ready may have reached the worker: release both.
            await self.disconnect(4502)
            await self.close(code=4502)
            return
        await self.accept()
        await self.send(text_data=json.dumps({"type": "preparing"}))
        self.upstream_task = asyncio.create_task(self._relay_upstream())

    async def _refuse(self, code, reason):
        logger.warning("Refusing assist session (%s): %s", code, reason)
        await self.close(code=code)

    async def _error(self, message):
        await self.send(text_data=json.dumps({"type": "error", "message": message}))

    async def receive(self, text_data=None, bytes_data=None):
        try:
            message = json.loads(text_data or "")
        except json.JSONDecodeError:
            message = None
        if not isinstance(message, dict) or message.get("type") != "prompt":
            await self._error("Unknown message.")
            return
        if not self.ready:
            await self._error("The assistant is still preparing this video.")
            return
        try:
            body = assist.prompt_body(self.session_id, message, self.probe)
            await asyncio.to_thread(assist.worker_request, "POST", "/api/session/prompt/", body)
        except assist.PromptError as exc:
            await self._error(str(exc))
        except Exception as exc:
            logger.warning("Assist prompt failed: %s", exc)
            await self._error("The assistant could not be reached.")

    async def disconnect(self, close_code):
        current = asyncio.current_task()
        if self.upstream_task and self.upstream_task is not current:
            self.upstream_task.cancel()
            # Whatever the relay died of, the cleanup below must still run.
            with suppress(asyncio.CancelledError, Exception):
                await self.upstream_task
        if self.upstream is not None:
            with suppress(Exception):
                await self.upstream.close()
            self.upstream = None
        if self.session_id:
            # Free the worker's RAM now, not after its grace period.
            with suppress(Exception):
                await asyncio.to_thread(
                    assist.worker_request, "DELETE", f"/api/session/{self.session_id}/"
                )
            self.session_id = None

    async def _relay_upstream(self):
        try:
            async for raw in self.upstream:
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                kind = message.get("type")
                if kind == "session_ready":
                    self.ready = True
                    await self.send(text_data=json.dumps({"type": "ready"}))
                elif kind == "frame_result":
                    await self.send(
                        text_data=json.dumps(
                            {
                                "type": "mask",
                                "frameIndex": message.get("frame_index"),
                                "mask": message.get("mask_b64"),
                                "shape": message.get("mask_shape"),
                            }
                        )
                    )
                elif kind == "inference_done":
                    await self.send(text_data=json.dumps({"type": "done"}))
                elif kind == "error":
                    await self._error(_WORKER_ERRORS.get(message.get("message"), "The assistant failed."))
        except websockets.ConnectionClosed:
            pass
        # Reached only when the worker ended the stream. When the browser leaves, the task is
        # cancelled instead, and closing the already-closed socket would raise in disconnect().
        await self.close(code=4502)
