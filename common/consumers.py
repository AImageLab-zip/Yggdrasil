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

from common.domains import DOMAINS
from common.permissions import user_can_write_patient_annotations


#: The languages the code knows how to ask for. A service row may narrow this
#: (``ExternalService.languages``); it cannot widen it past what Whisper supports.
SUPPORTED_LANGUAGES = frozenset({"it", "en", "es", "fr", "de"})
MAX_AUDIO_FRAME_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


@database_sync_to_async
def _config():
    """The speech-to-text endpoint to relay to, or ``None`` if there is none.

    An admin row wins; with no row, ``settings.WHISPER_*`` is used exactly as it was
    before the registry existed. Wrapped for the ORM read because this runs inside the
    consumer's async ``connect``.
    """
    from common.external_config import whisper_service

    return whisper_service()


def _supported_languages(config):
    """What this endpoint accepts: its own list, else the codebase default."""
    declared = frozenset(config.languages) & SUPPORTED_LANGUAGES if config else frozenset()
    return declared or SUPPORTED_LANGUAGES


def _upstream_url(config, language):
    parts = urlsplit(config.base_url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["token"] = [config.api_key]
    query["lang"] = [language]
    encoded = urlencode(query, doseq=True, quote_via=quote)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, encoded, parts.fragment))


def _ssl_context(url, ca_cert=None):
    """The SSL context for this upstream, or ``None`` when it speaks plain ``ws://``.

    The pinned-CA requirement belongs to the *scheme*, not to the feature. A service row
    may name a ``ws://`` upstream -- a speech-to-text service on an internal network,
    reached by service name -- where there is no certificate to pin and demanding one
    would refuse every connection. A ``wss://`` upstream is unchanged: it still requires a
    CA and still fails rather than falling back to the system trust store, because a
    self-signed certificate is not in it.
    """
    if urlsplit(url).scheme != "wss":
        return None
    cafile = ca_cert if ca_cert is not None else settings.WHISPER_CA_CERT
    if not cafile:
        raise RuntimeError("WHISPER_CA_CERT is not configured")
    return ssl.create_default_context(cafile=cafile)


@database_sync_to_async
def _can_transcribe(user, domain, patient_id):
    """Whether ``user`` may dictate into this patient's captions.

    A caption is annotation work, so this is the same check the HTTP caption endpoints
    make, through the same helper -- and deliberately not a second spelling of it.

    **The project is the patient's, not the domain's.** This used to pass the domain
    *slug* as the permission context, and a string resolves through
    ``entry_project_for(None, domain)`` (``common.permissions._project_from_context``):
    the domain's first active project by name, for nobody in particular. Every patient in
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

        # Config is read after the permission check on purpose: whether this deployment
        # has speech-to-text configured is not something to tell a user who may not
        # dictate on this patient anyway.
        config = await _config()
        if config is None:
            await self._refuse(
                4503,
                "no speech-to-text service is configured (no enabled ExternalService row, "
                "and WHISPER_WS_URL/WHISPER_API_TOKEN are unset)",
            )
            return
        if language not in _supported_languages(config):
            await self._refuse(
                4400,
                f"{language!r} is not in the language set of service {config.slug!r}",
            )
            return

        url = _upstream_url(config, language)
        try:
            self.upstream = await websockets.connect(
                url,
                ssl=_ssl_context(url, config.ca_cert),
                open_timeout=config.connect_timeout,
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
