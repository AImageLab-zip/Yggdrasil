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
from common.permissions import user_can_write_annotations, user_is_project_admin


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
    if domain not in DOMAINS or not user or not user.is_authenticated:
        return False

    Patient = apps.get_model(domain, "Patient")
    patient = Patient.objects.filter(patient_id=patient_id).first()
    if patient is None:
        return False
    if user_is_project_admin(user, domain):
        return True

    if any(field.name == "folders" for field in Patient._meta.get_fields()):
        folders = patient.folders.all()
    else:
        folder = getattr(patient, "folder", None)
        folders = [folder] if folder else []
    return any(user_can_write_annotations(user, folder, domain) for folder in folders)


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
            await self.close(code=4401)
            return
        if language not in SUPPORTED_LANGUAGES:
            await self.close(code=4400)
            return
        if not await _can_transcribe(user, domain, patient_id):
            await self.close(code=4403)
            return
        if not settings.WHISPER_API_TOKEN or not settings.WHISPER_WS_URL:
            await self.close(code=4503)
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
