import asyncio
import base64
import json
from unittest import mock

from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.apps import apps
from django.contrib.auth.models import AnonymousUser, User
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from common import assist
from common.consumers import AssistSessionConsumer
from common.models import AnnotationMethod, FileRegistry, Project, ProjectAccess
from common.routing import websocket_urlpatterns

PROBE = {"width": 16, "height": 8, "fps": 25.0, "frame_count": 100}


class PromptBodyTests(SimpleTestCase):
    def body(self, **message):
        base = {"frameIndex": 3, "points": [[2, 1]], "labels": [1]}
        return assist.prompt_body("lap-1-0a1b2c3d", {**base, **message}, PROBE)

    def test_a_click_becomes_one_region_on_that_frame_only(self):
        self.assertEqual(
            self.body(),
            {
                "video_id": "lap-1-0a1b2c3d",
                "frame_index": 3,
                "window_seconds": 0,
                "normalized": False,
                "regions": [{"region_id": "1", "points": [[2.0, 1.0]], "point_labels": [1]}],
            },
        )

    def test_a_window_tracks_exactly_that_many_frames_forward(self):
        body = self.body(window=5)
        # The worker tracks int(window_seconds * fps) frames after the prompted one.
        self.assertEqual(int(body["window_seconds"] * PROBE["fps"]), 5)
        self.assertEqual(self.body(window=0)["window_seconds"], 0)
        thirds = assist.prompt_body("lap-1-0a1b2c3d", {"frameIndex": 3, "points": [[2, 1]], "labels": [1], "window": 5}, {**PROBE, "fps": 3.0})
        self.assertEqual(int(thirds["window_seconds"] * 3.0), 5, "no float rounding down to 4")

    def test_the_current_mask_rides_along_for_refinement(self):
        mask = base64.b64encode(bytes(16)).decode()  # 16 x 8 pixels = 16 bytes
        region = self.body(mask=mask)["regions"][0]
        self.assertEqual(
            (region["mask_b64"], region["mask_encoding"], region["mask_shape"]),
            (mask, "bitpack_u1_v1", [8, 16]),
        )

    def test_what_the_worker_must_not_see_is_refused(self):
        for message in (
            {"frameIndex": 100}, {"frameIndex": -1}, {"frameIndex": "3"}, {"frameIndex": True},
            {"points": []}, {"points": [[0, 0]] * 65, "labels": [1] * 65},
            {"points": [[16, 0]]}, {"points": [[0, 8]]}, {"points": [[-1, 0]]}, {"points": [[1]]},
            {"points": [["1", 1]]}, {"labels": [2]}, {"labels": [1, 0]}, {"labels": [True]},
            {"mask": "not base64!"}, {"mask": base64.b64encode(bytes(15)).decode()},
            {"window": -1}, {"window": 51}, {"window": "5"}, {"window": True},
        ):
            with self.subTest(message=message), self.assertRaises(assist.PromptError):
                self.body(**message)


class WorkerAddressTests(SimpleTestCase):
    def test_each_session_id_is_new_and_names_the_file(self):
        first, second = assist.new_session_id(7), assist.new_session_id(7)
        self.assertRegex(first, r"^lap-7-[0-9a-f]{8}$")
        self.assertNotEqual(first, second)

    @override_settings(ASSIST_WORKER_URL="https://worker.example:8443/", ASSIST_WORKER_TOKEN="s3cret")
    def test_the_socket_follows_the_http_scheme_and_carries_the_token(self):
        self.assertEqual(
            assist.upstream_ws_url("lap-7-0a1b2c3d"),
            "wss://worker.example:8443/ws/session/lap-7-0a1b2c3d/",
        )
        self.assertEqual(assist.auth_headers(), {"Authorization": "Bearer s3cret"})

    @override_settings(ASSIST_WORKER_URL="file:///etc/passwd", ASSIST_WORKER_TOKEN="t")
    def test_only_an_http_worker_url_is_ever_opened(self):
        with self.assertRaises(ValueError):
            assist.worker_request("GET", "/")
        with self.assertRaises(ValueError):
            assist.upstream_ws_url("lap-1-0a1b2c3d")

    def test_a_probe_must_be_complete(self):
        class File:
            def __init__(self, metadata):
                self.metadata = metadata

        good = {"probe": {"width": 16, "height": 8, "fps": 25.0, "frame_count": 100}}
        self.assertEqual(assist.probe_of(File(good)), PROBE)
        for metadata in (None, {}, {"probe": {"width": 16}}, {"probe": {**good["probe"], "width": 0}}):
            with self.subTest(metadata=metadata):
                self.assertIsNone(assist.probe_of(File(metadata)))


KEY = "laparoscopy/processed/video/job_7/subsampled.mp4"


class FakeWorkerSocket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.closed = False

    async def push(self, message):
        await self.messages.put(json.dumps(message))

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


async def _until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


@override_settings(ASSIST_WORKER_URL="http://djangoworker-api:8000", ASSIST_WORKER_TOKEN="s3cret")
class AssistSessionConsumerTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        Patient = apps.get_model("laparoscopy", "Patient")
        Folder = apps.get_model("laparoscopy", "Folder")
        self.project = Project.objects.create(slug="lap-assist", name="Lap assist", domain="laparoscopy")
        self.method, _ = AnnotationMethod.objects.get_or_create(
            slug=assist.METHOD_SLUG, defaults={"name": "Assisted Segmentation", "domain": "laparoscopy"}
        )
        self.project.annotation_methods.add(self.method)
        self.user = User.objects.create_user(username="annotator", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="annotator")
        folder = Folder.objects.create(name="F", project=self.project)
        self.patient = Patient.objects.create(name="P", folder=folder, project=self.project)
        self.video = FileRegistry.objects.create(
            domain="laparoscopy", file_type="video_processed", subtype="subsampled",
            laparoscopy_patient=self.patient, file_path=KEY, file_size=1, file_hash="a" * 64,
            metadata={"probe": {"width": 16, "height": 8, "fps": 25.0, "frame_count": 100}},
        )
        self.upstreams = []
        self.calls = []

        async def connect(url, **kwargs):
            socket = FakeWorkerSocket()
            socket.url, socket.kwargs = url, kwargs
            self.upstreams.append(socket)
            return socket

        for patcher in (
            mock.patch("common.consumers.websockets.connect", new=connect),
            mock.patch("common.assist.worker_request", side_effect=lambda *call: self.calls.append(call) or 202),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def communicator(self, user=None, file_id=None):
        communicator = WebsocketCommunicator(
            URLRouter(websocket_urlpatterns),
            f"/ws/assist/laparoscopy/{self.patient.patient_id}/?fileId={file_id or self.video.pk}",
        )
        communicator.scope["user"] = user or self.user
        return communicator

    async def connected(self):
        communicator = self.communicator()
        ok, _ = await communicator.connect()
        self.assertTrue(ok)
        self.assertEqual(await communicator.receive_json_from(), {"type": "preparing"})
        return communicator

    async def refused(self, communicator):
        ok, code = await communicator.connect()
        self.assertFalse(ok)
        return code

    async def test_refuses_anonymous(self):
        self.assertEqual(await self.refused(self.communicator(AnonymousUser())), 4401)

    async def test_refuses_a_viewer(self):
        await database_sync_to_async(ProjectAccess.objects.filter(user=self.user).update)(role="viewer")
        self.assertEqual(await self.refused(self.communicator()), 4403)

    async def test_refuses_when_the_project_has_the_method_off(self):
        await database_sync_to_async(self.project.annotation_methods.remove)(self.method)
        self.assertEqual(await self.refused(self.communicator()), 4403)

    async def test_refuses_when_no_worker_is_configured(self):
        with self.settings(ASSIST_WORKER_TOKEN=""):
            self.assertEqual(await self.refused(self.communicator()), 4503)

    async def test_refuses_a_file_that_is_not_this_patients_subsampled_video(self):
        raw = await database_sync_to_async(FileRegistry.objects.create)(
            domain="laparoscopy", file_type="video_raw", laparoscopy_patient=self.patient,
            file_path="laparoscopy/raw/case.mp4", file_size=1, file_hash="b" * 64,
        )
        self.assertEqual(await self.refused(self.communicator(file_id=raw.pk)), 4404)
        self.assertEqual(await self.refused(self.communicator(file_id=999999)), 4404)
        self.assertEqual(self.calls, [], "the worker heard nothing")

    async def test_opens_a_worker_session_on_the_patients_own_video_and_closes_it_on_leave(self):
        communicator = await self.connected()
        method, path, body = self.calls[0]
        self.assertEqual((method, path, body["video_source"]), ("POST", "/api/session/ready/", KEY))
        session_id = body["video_id"]
        self.assertRegex(session_id, rf"^lap-{self.video.pk}-[0-9a-f]{{8}}$")
        upstream = self.upstreams[0]
        self.assertEqual(upstream.url, f"ws://djangoworker-api:8000/ws/session/{session_id}/")
        self.assertEqual(upstream.kwargs["additional_headers"], {"Authorization": "Bearer s3cret"})

        await communicator.disconnect()
        await _until(lambda: self.calls[-1][:2] == ("DELETE", f"/api/session/{session_id}/"))
        self.assertTrue(upstream.closed)

    async def test_leaving_frees_the_worker_session_even_when_the_socket_is_already_gone(self):
        # Uvicorn refuses a second close once the browser has left ("Unexpected ASGI message
        # 'websocket.close'"); that must not stop the worker session from being freed.
        communicator = await self.connected()
        session_id = self.calls[0][2]["video_id"]
        refuse = RuntimeError("Unexpected ASGI message 'websocket.close'")
        with mock.patch.object(AssistSessionConsumer, "close", side_effect=refuse):
            try:
                await communicator.disconnect()
            except RuntimeError:
                pass
            await _until(lambda: self.calls[-1][:2] == ("DELETE", f"/api/session/{session_id}/"))
        self.assertTrue(self.upstreams[0].closed)

    async def test_each_connection_gets_its_own_worker_session(self):
        first = await self.connected()
        await first.disconnect()
        second = await self.connected()
        ready = [call[2]["video_id"] for call in self.calls if call[1] == "/api/session/ready/"]
        self.assertEqual(len(set(ready)), 2)
        await second.disconnect()

    async def test_a_prompt_before_the_worker_is_ready_is_refused(self):
        communicator = await self.connected()
        await communicator.send_json_to({"type": "prompt", "frameIndex": 4, "points": [[1, 1]], "labels": [1]})
        self.assertEqual(
            await communicator.receive_json_from(),
            {"type": "error", "message": "The assistant is still preparing this video."},
        )
        self.assertFalse(any(call[1] == "/api/session/prompt/" for call in self.calls))
        await communicator.disconnect()

    async def test_relays_a_prompt_and_its_mask(self):
        communicator = await self.connected()
        upstream = self.upstreams[0]
        await upstream.push({"type": "session_ready"})
        self.assertEqual(await communicator.receive_json_from(), {"type": "ready"})

        await communicator.send_json_to({"type": "prompt", "frameIndex": 4, "points": [[1, 1]], "labels": [1]})
        await _until(lambda: self.calls[-1][1] == "/api/session/prompt/")
        sent = self.calls[-1][2]
        self.assertEqual((sent["frame_index"], sent["window_seconds"]), (4, 0))

        await upstream.push({"type": "frame_result", "frame_index": 4, "mask_b64": "AA==", "mask_shape": [8, 16], "region_id": "1"})
        self.assertEqual(
            await communicator.receive_json_from(),
            {"type": "mask", "frameIndex": 4, "mask": "AA==", "shape": [8, 16]},
        )
        await upstream.push({"type": "inference_done", "job_id": "j1"})
        self.assertEqual(await communicator.receive_json_from(), {"type": "done"})
        await communicator.disconnect()

    async def test_a_bad_prompt_is_explained_and_never_forwarded(self):
        communicator = await self.connected()
        await self.upstreams[0].push({"type": "session_ready"})
        await communicator.receive_json_from()
        await communicator.send_json_to({"type": "prompt", "frameIndex": 4, "points": [[99, 1]], "labels": [1]})
        self.assertEqual(
            await communicator.receive_json_from(),
            {"type": "error", "message": "A point lies outside the frame."},
        )
        self.assertFalse(any(call[1] == "/api/session/prompt/" for call in self.calls))
        await communicator.disconnect()

    async def test_worker_errors_are_translated_never_passed_through(self):
        communicator = await self.connected()
        upstream = self.upstreams[0]
        await upstream.push({"type": "error", "message": "inference_failed"})
        self.assertEqual(
            await communicator.receive_json_from(),
            {"type": "error", "message": "The assistant could not segment this frame."},
        )
        await upstream.push({"type": "error", "message": "Traceback: /app/secret/path"})
        self.assertEqual(
            await communicator.receive_json_from(),
            {"type": "error", "message": "The assistant failed."},
        )
        await communicator.disconnect()

    async def test_a_dead_worker_closes_the_browser_socket(self):
        communicator = await self.connected()
        await self.upstreams[0].close()
        self.assertEqual(await communicator.receive_output(), {"type": "websocket.close", "code": 4502})
