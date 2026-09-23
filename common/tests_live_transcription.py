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


@override_settings(
    WHISPER_API_TOKEN="test-token",
    WHISPER_CA_CERT="/unused/test-ca.pem",
)
class UpstreamSchemeTests(TransactionTestCase):
    """Which upstreams get a pinned CA, and which must not be asked for one.

    ``_ssl_context`` used to demand ``WHISPER_CA_CERT`` unconditionally, which was right
    while the only upstream was an external ``wss://`` host with a self-signed
    certificate. A service row may now name a ``ws://`` upstream on an internal
    network, where there is no certificate to pin -- and an unconditional context
    would refuse every connection to it.
    """

    reset_sequences = True

    def test_a_plain_ws_upstream_gets_no_ssl_context(self):
        from common.consumers import _ssl_context

        self.assertIsNone(_ssl_context("ws://whisper:9097/ws"))

    @override_settings(WHISPER_CA_CERT="")
    def test_a_plain_ws_upstream_does_not_need_a_ca_at_all(self):
        """A ws:// upstream must work on a deployment that never had a CA file."""
        from common.consumers import _ssl_context

        self.assertIsNone(_ssl_context("ws://whisper:9097/ws"))

    def test_a_wss_upstream_still_pins_the_ca(self):
        from common.consumers import _ssl_context

        with mock.patch("common.consumers.ssl.create_default_context") as create:
            _ssl_context("wss://155.185.48.254:9097/ws")
        create.assert_called_once_with(cafile="/unused/test-ca.pem")

    @override_settings(WHISPER_CA_CERT="")
    def test_a_wss_upstream_without_a_ca_still_refuses(self):
        """No silent fall back to the system trust store: that cert is not in it."""
        from common.consumers import _ssl_context

        with self.assertRaises(RuntimeError):
            _ssl_context("wss://155.185.48.254:9097/ws")


@override_settings(
    WHISPER_WS_URL="ws://whisper:9097/ws",
    WHISPER_API_TOKEN="test-token",
    WHISPER_CA_CERT="",
)
class LocalWhisperServiceTests(TransactionTestCase):
    """End to end against the shipped container's configuration shape."""

    reset_sequences = True

    def setUp(self):
        self.project, _ = Project.objects.get_or_create(
            slug="maxillo", defaults={"name": "Maxillo"}
        )
        self.user = User.objects.create_user(username="local-transcriber", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.patient = Patient.objects.create(name="P", project=self.project)

    async def test_connects_to_the_local_service_without_a_certificate(self):
        upstream = FakeWhisperSocket()
        with mock.patch(
            "common.consumers.websockets.connect",
            new=mock.AsyncMock(return_value=upstream),
        ) as connect:
            communicator = WebsocketCommunicator(
                URLRouter(websocket_urlpatterns),
                f"/ws/live-transcription/maxillo/{self.patient.patient_id}/?lang=it",
            )
            communicator.scope["user"] = self.user
            connected, code = await communicator.connect()
            self.assertTrue(connected, msg=f"refused with {code}")
            self.assertEqual(await communicator.receive_json_from(), {"type": "ready"})

            self.assertTrue(connect.await_args.args[0].startswith("ws://whisper:9097/ws"))
            self.assertIsNone(connect.await_args.kwargs["ssl"])
            await communicator.disconnect()


@override_settings(
    WHISPER_WS_URL="wss://external.example/ws",
    WHISPER_API_TOKEN="env-token",
    WHISPER_CA_CERT="/unused/test-ca.pem",
)
class RelayReadsTheServiceRowTests(TransactionTestCase):
    """The relay's endpoint is admin-owned; ``settings`` is only the fallback.

    The pairing that matters is the last test: a disabled row must not fall back to the
    environment. If it did, an admin who turns dictation off would watch it keep working
    and have no way to stop it, which makes the tick box decorative.
    """

    reset_sequences = True

    def setUp(self):
        from common.models import ExternalService

        self.project, _ = Project.objects.get_or_create(
            slug="maxillo", defaults={"name": "Maxillo"}
        )
        self.user = User.objects.create_user(username="row-transcriber", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.patient = Patient.objects.create(name="P", project=self.project)
        self.service = ExternalService.objects.create(
            slug="whisper_live",
            name="Local Whisper",
            kind="stt_websocket",
            base_url="ws://whisper:9097/ws",
            api_key_env="TEST_RELAY_TOKEN",
            languages=["it", "en"],
            connect_timeout_seconds=7,
        )

    def communicator(self, query="lang=it"):
        communicator = WebsocketCommunicator(
            URLRouter(websocket_urlpatterns),
            f"/ws/live-transcription/maxillo/{self.patient.patient_id}/?{query}",
        )
        communicator.scope["user"] = self.user
        return communicator

    async def test_the_row_overrides_the_environment(self):
        upstream = FakeWhisperSocket()
        with (
            mock.patch.dict("os.environ", {"TEST_RELAY_TOKEN": "row-token"}),
            mock.patch(
                "common.consumers.websockets.connect",
                new=mock.AsyncMock(return_value=upstream),
            ) as connect,
        ):
            communicator = self.communicator()
            connected, code = await communicator.connect()
            self.assertTrue(connected, msg=f"refused with {code}")

            url = connect.await_args.args[0]
            self.assertTrue(url.startswith("ws://whisper:9097/ws"))
            self.assertIn("token=row-token", url)
            self.assertNotIn("env-token", url)
            self.assertEqual(connect.await_args.kwargs["open_timeout"], 7)
            await communicator.disconnect()

    async def test_a_language_outside_the_rows_set_is_refused(self):
        """The row narrows what the endpoint is asked for; 'de' is supported code-side."""
        with mock.patch.dict("os.environ", {"TEST_RELAY_TOKEN": "row-token"}):
            communicator = self.communicator("lang=de")
            connected, code = await communicator.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4400)

    async def test_a_disabled_row_refuses_even_though_the_environment_is_set(self):
        from common.models import ExternalService

        await database_sync_to_async(
            ExternalService.objects.filter(slug="whisper_live").update
        )(is_enabled=False)

        with mock.patch.dict("os.environ", {"TEST_RELAY_TOKEN": "row-token"}):
            communicator = self.communicator()
            connected, code = await communicator.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4503)
