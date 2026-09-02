import asyncio
import json
from unittest import mock

from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser, User
from django.test import TransactionTestCase, override_settings

from common.models import Project, ProjectAccess
from common.routing import websocket_urlpatterns
from maxillo.models import Patient


class FakeWhisperSocket:
    def __init__(self):
        self.sent = []
        self.sent_event = asyncio.Event()
        self.messages = asyncio.Queue()
        self.closed = False

    async def send(self, data):
        self.sent.append(data)
        self.sent_event.set()

    async def close(self):
        self.closed = True
        await self.messages.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        return message


@override_settings(
    WHISPER_WS_URL="wss://155.185.48.254:9097/ws",
    WHISPER_API_TOKEN="test-token",
    WHISPER_CA_CERT="/unused/test-ca.pem",
)
class LiveTranscriptionConsumerTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.project, _ = Project.objects.get_or_create(
            slug="maxillo", defaults={"name": "Maxillo"}
        )
        self.user = User.objects.create_user(username="transcriber", password="x")
        self.outsider = User.objects.create_user(username="outsider", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.patient = Patient.objects.create(name="P", project=self.project)

    def communicator(self, user, query="lang=it"):
        communicator = WebsocketCommunicator(
            URLRouter(websocket_urlpatterns),
            f"/ws/live-transcription/maxillo/{self.patient.patient_id}/?{query}",
        )
        communicator.scope["user"] = user
        return communicator

    async def test_rejects_anonymous_user(self):
        communicator = self.communicator(AnonymousUser())
        connected, code = await communicator.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4401)

    async def test_rejects_unsupported_language(self):
        communicator = self.communicator(self.user, "lang=xx")
        connected, code = await communicator.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4400)

    async def test_relays_pcm_and_transcripts(self):
        upstream = FakeWhisperSocket()
        with (
            mock.patch("common.consumers._ssl_context"),
            mock.patch(
                "common.consumers.websockets.connect",
                new=mock.AsyncMock(return_value=upstream),
            ) as connect,
        ):
            communicator = self.communicator(self.user, "lang=de")
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            self.assertEqual(await communicator.receive_json_from(), {"type": "ready"})

            await communicator.send_to(bytes_data=b"\x01\x00\x02\x00")
            await asyncio.wait_for(upstream.sent_event.wait(), timeout=1)
            self.assertEqual(upstream.sent, [b"\x01\x00\x02\x00"])

            await upstream.messages.put(json.dumps({"type": "partial", "text": "Hallo"}))
            self.assertEqual(
                await communicator.receive_json_from(),
                {"type": "partial", "text": "Hallo"},
            )
            await upstream.messages.put(json.dumps({"type": "final", "text": "Hallo Welt"}))
            self.assertEqual(
                await communicator.receive_json_from(),
                {"type": "final", "text": "Hallo Welt"},
            )

            url = connect.await_args.args[0]
            self.assertIn("token=test-token", url)
            self.assertIn("lang=de", url)
            await communicator.disconnect()
            self.assertTrue(upstream.closed)

    async def test_rejects_user_without_annotation_access(self):
        communicator = self.communicator(self.outsider)
        connected, code = await communicator.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4403)

    async def test_allows_a_patient_outside_the_domain_first_project(self):
        """The project checked is the patient's, not the domain's first by name.

        This is the case `_can_transcribe` used to get wrong, and the one a deployment
        with more than one project per domain is made of: the consumer resolved the
        permission context from the domain *slug*, which `_project_from_context` turns
        into `entry_project_for(None, domain)` -- the first active project of the domain
        by name. A patient in any other project was therefore checked against a project it
        is not in, and every attempt closed 4403 while the patient page itself, which
        resolves the project the normal way, kept working.
        """
        other = await database_sync_to_async(Project.objects.create)(
            name="A Other", slug="maxillo-other", domain="maxillo"
        )
        # Named after "A Other" so it is *not* the one a domain-wide lookup would find,
        # and holding no access for the user at all.
        self.assertLess(other.name, self.project.name)

        upstream = FakeWhisperSocket()
        with (
            mock.patch("common.consumers._ssl_context"),
            mock.patch(
                "common.consumers.websockets.connect",
                new=mock.AsyncMock(return_value=upstream),
            ),
        ):
            communicator = self.communicator(self.user)
            connected, code = await communicator.connect()
            self.assertTrue(connected, msg=f"refused with {code}")
            await communicator.disconnect()

    async def test_rejects_a_writer_of_another_project_in_the_domain(self):
        """Access to one project of a domain is not access to its other patients.

        The other half of the same fix: resolving the project from the domain also meant
        an admin of whichever project sorted first could dictate into every patient of the
        domain, including projects they hold nothing on.
        """
        other = await database_sync_to_async(Project.objects.create)(
            name="A Other", slug="maxillo-other", domain="maxillo"
        )
        await database_sync_to_async(ProjectAccess.objects.create)(
            user=self.outsider, project=other, role="admin"
        )

        communicator = self.communicator(self.outsider)
        connected, code = await communicator.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4403)
