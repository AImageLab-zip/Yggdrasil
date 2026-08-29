"""The laparoscopy video surface, as the patient-detail page wires it.

Roadmap Phase 10 replaced 4,919 lines of Konva -- `laparoscopy_annotator.js` and its six
mixins -- with the `video-annotate` bundle entry, and removed both Konva CDN tags, so no
page in this repository loads Konva any more.

Phase 5's lesson is why this module exists: **a template id joining two files is an
untested interface**, and a rename leaves the JS holding `None` and a control stuck in
whatever state the template shipped -- silently, and only on the surface being replaced.

The other property asserted here is the one that decides whether a mask lands on the
right pixels: the page must state the video's frame size and frame rate, and must refuse
to mount the annotator when it cannot. A browser cannot read a video's frame rate, so
guessing 30 for a 25 fps recording would put every annotation on the wrong frame and look
entirely correct while doing it.
"""

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from common.models import (
    AnnotationMethod,
    FileRegistry,
    Modality,
    Project,
    ProjectAccess,
)
from laparoscopy.models import Folder, Patient

#: The ids `frontend/imaging/video/bootstrap.js` and the page's glue look up by name.
CONTROL_IDS = (
    "videoAnnotateData",
    "video-annotate-viewport",
    "annotation-toolbar",
    "annotation-toggle-btn",
    "frame-nav-bar",
    "temporal-classification-bar",
)

#: The toolbar keys `frontend/imaging/video/editor.js` TOOL_PLAN answers to.
TOOL_KEYS = ("brush", "eraser", "polygon", "pan")


@override_settings(SECURE_SSL_REDIRECT=False)
class VideoSurfaceRenderTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.update_or_create(
            slug="laparoscopy",
            defaults={"name": "Laparoscopy", "domain": "laparoscopy"},
        )
        self.modality, _ = Modality.objects.get_or_create(
            slug="video", defaults={"name": "Video"}
        )
        self.project.modalities.set([self.modality])
        # The toolbar is gated on the project enabling the method, and the write
        # endpoint re-checks it. Enabling it here exercises the real path; without it
        # the page renders correctly *and* without an annotator, which is a legitimate
        # state and not the one under test.
        method, _ = AnnotationMethod.objects.get_or_create(
            slug="video_regions",
            defaults={"name": "Video region annotation", "domain": "laparoscopy"},
        )
        self.project.annotation_methods.add(method)

        self.user = User.objects.create_user(username="lap-render", password="x")  # noqa: S106
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)

        self.folder = Folder.objects.create(name="Cases", project=self.project)
        self.patient = Patient.objects.create(
            name="Case", folder=self.folder, project=self.project
        )

    def _video(self, *, probe=True):
        metadata = {"original_filename": "op.mp4"}
        if probe:
            metadata["probe"] = {
                "width": 1920,
                "height": 1080,
                "fps": 25.0,
                "frame_count": 500,
            }
        return FileRegistry.objects.create(
            domain="laparoscopy",
            laparoscopy_patient=self.patient,
            file_type="video_raw",
            file_path=f"laparoscopy/patient_{self.patient.patient_id}/op.mp4",
            file_size=1024,
            file_hash="videohash",
            modality=self.modality,
            metadata=metadata,
        )

    def _page(self):
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()
        response = self.client.get(
            reverse(
                "laparoscopy:patient_detail",
                kwargs={"patient_id": self.patient.patient_id},
            )
        )
        return response, response.content.decode()

    # --- the wiring ---------------------------------------------------------------

    def test_every_id_the_surface_resolves_is_in_the_markup(self):
        self._video()
        _response, html = self._page()
        for element_id in CONTROL_IDS:
            self.assertIn(f'id="{element_id}"', html, element_id)

    def test_every_toolbar_key_the_tool_plan_answers_to_is_in_the_markup(self):
        self._video()
        _response, html = self._page()
        for key in TOOL_KEYS:
            self.assertIn(f'data-tool="{key}"', html, key)

    def test_the_video_entry_is_loaded(self):
        self._video()
        _response, html = self._page()
        self.assertIn("video-annotate", html)

    def test_konva_is_gone(self):
        """The last consumer went with Phase 10, and nothing must bring the tag back."""
        self._video()
        _response, html = self._page()
        self.assertNotIn("konva.min.js", html.lower())

    def test_the_deleted_annotator_is_not_referenced(self):
        self._video()
        _response, html = self._page()
        for script in (
            "laparoscopy_annotator.js",
            "laparoscopy_annotator_shapes.js",
            "laparoscopy_annotator_mask.js",
            "laparoscopy_annotator_magic.js",
            "laparoscopy_annotator_timeline.js",
            "laparoscopy_annotator_utils.js",
            "laparoscopy_annotator_api.js",
            "laparoscopy_annotator_worker.js",
        ):
            self.assertNotIn(script, html, script)

    # --- the payload, which decides which pixels a mask describes -------------------

    def test_the_payload_states_the_frame_size_and_rate_the_server_probed(self):
        import json

        self._video()
        _response, html = self._page()
        start = html.index('id="videoAnnotateData"')
        body = html[html.index(">", start) + 1 : html.index("</script>", start)]
        payload = json.loads(body)
        self.assertEqual(payload["width"], 1920)
        self.assertEqual(payload["height"], 1080)
        self.assertEqual(payload["fps"], 25.0)
        self.assertEqual(payload["frameCount"], 500)
        self.assertEqual(payload["patientId"], self.patient.patient_id)
        self.assertIn("video-annotations", payload["endpoint"])

    def test_a_video_with_no_recorded_probe_does_not_mount_the_annotator(self):
        """Rather than guess a frame rate, which would mis-file every mask.

        A browser cannot read a video's frame rate. Mounting anyway and defaulting to 30
        would put every annotation of a 25 fps recording on the wrong frame, and nothing
        in the interface would look wrong.
        """
        self._video(probe=False)
        _response, html = self._page()
        self.assertIn("videoAnnotateData", html)
        start = html.index('id="videoAnnotateData"')
        body = html[html.index(">", start) + 1 : html.index("</script>", start)]
        self.assertEqual(body.strip(), "null")

    def test_the_toolbar_is_hidden_when_the_project_disables_the_method(self):
        """The gate is the project registry, and the page has to honour it."""
        self.project.annotation_methods.clear()
        self._video()
        _response, html = self._page()
        self.assertNotIn('id="annotation-toggle-btn"', html)

    def test_a_patient_with_no_video_renders_without_the_surface(self):
        response, html = self._page()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('id="video-annotate-viewport"', html)
