"""The intraoral photograph surface, as the patient-detail page wires it.

Roadmap Phase 5 replaced two files with one Cornerstone entry: `modality_viewers/intraoral.js`
(a grid of ``<img>`` thumbnails) and `intraoral_segmentation.js` (1901 lines of Konva). The
page now carries *two* photo-stack payloads -- teleradiography and the intraoral
photographs -- and one entry mounts both.

That wiring is only ever exercised by rendering the page, which nothing did before this
module. A missing context variable, a payload under the wrong element id, or a script tag
left behind would all leave the tab blank in a browser and green in CI.
"""

import json

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
from maxillo.models import Folder, Patient


@override_settings(SECURE_SSL_REDIRECT=False)
class IntraoralSurfaceRenderTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.update_or_create(
            slug="maxillo", defaults={"name": "maxillo", "domain": "maxillo"}
        )
        self.modality, _ = Modality.objects.get_or_create(
            slug="intraoral-photo", defaults={"name": "Intraoral Photographs"}
        )
        method, _ = AnnotationMethod.objects.get_or_create(
            slug="intraoral_segmentation",
            defaults={"name": "Intraoral Segmentation"},
        )
        self.project.modalities.set([self.modality])
        self.project.annotation_methods.set([method])

        self.user = User.objects.create_user(username="seg-render", password="x")  # noqa: S106
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)

        self.folder = Folder.objects.create(name="Photos", project=self.project)
        self.patient = Patient.objects.create(
            patient_id=9801, name="Photo patient", folder=self.folder, project=self.project
        )
        self.patient.modalities.add(self.modality)
        FileRegistry.objects.create(
            patient=self.patient,
            domain="maxillo",
            file_type="intraoral_raw",
            file_path="maxillo/intraoral_raw/a.jpg",
            file_size=4,
            file_hash="e" * 64,
            modality=self.modality,
        )

    def _page(self):
        response = self.client.get(
            reverse("maxillo:patient_detail", kwargs={"patient_id": self.patient.patient_id})
        )
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode("utf-8")

    def test_the_page_carries_a_separate_payload_for_each_photo_stack(self):
        """Two surfaces, two element ids.

        Both mount on one page and ids are unique per *document*, so a shared
        ``photoStackData`` would have the intraoral stack resolve teleradiography's
        payload and both render into one element.
        """
        response, html = self._page()
        self.assertIn('id="photoStackData"', html)
        self.assertIn('id="intraoralStackData"', html)

        payload = response.context["intraoral_stack_data"]
        self.assertEqual(payload["patientId"], self.patient.patient_id)
        self.assertIs(payload["segmentation"], True)
        self.assertEqual(
            payload["endpoint"], f"/maxillo/api/patient/{self.patient.patient_id}/intraoral/"
        )
        # An admin may edit; the server refuses regardless, and this only decides whether
        # the editor offers an action that would then fail.
        self.assertIs(payload["canModify"], True)
        # It has to survive `json_script`, which is what the bootstrap parses.
        self.assertIn(json.dumps(payload["endpoint"])[1:-1], html)

    def test_the_intraoral_viewport_and_tooth_workbench_are_present(self):
        _response, html = self._page()
        for element_id in (
            "intraoralPhotoViewport",
            "intraoralPhotoAnnotationMode",
            "intraoralPhotoAnnotationTools",
            "intraoralPhotoCalibrate",
            "segMode",
            "segTools",
            "segTeethGrid",
            "segConfirmBtn",
            "segUndoBtn",
            "segRedoBtn",
            "segStatusText",
        ):
            self.assertIn(f'id="{element_id}"', html, element_id)

    def test_both_toolbars_hide_their_calibration_group_until_measure_is_on(self):
        """Calibrate only works after a Length line exists, so it belongs with the tools.

        The ids are asserted here as well as in `controls.js` because the two halves are
        joined by a string: a renamed id leaves the JS quietly holding `null` and the
        control stuck in whichever state the template shipped. That is precisely how the
        tooth grid came to be permanently invisible -- a CSS class nothing set any more.
        """
        _response, html = self._page()
        for element_id in ("intraoralPhotoCalibrationGroup", "photoCalibrationGroup"):
            self.assertIn(f'id="{element_id}"', html, element_id)
            group = html.split(f'id="{element_id}"', 1)[1].split(">", 1)[0]
            self.assertIn("hidden", group, element_id)

    def test_the_intraoral_surface_carries_its_own_csrf_token(self):
        """`CSRF_USE_SESSIONS` is on, so the hidden input is the only source.

        Borrowing the teleradiography branch's works right up until that branch stops
        rendering, at which point every save is a bare 403 with Django's HTML error page --
        which is exactly how the volume grid's first version failed.
        """
        _response, html = self._page()
        viewer = html.split('id="intraoral-viewer"', 1)[1].split("</div>", 40)[0]
        self.assertIn("csrfmiddlewaretoken", viewer)

    def test_the_replaced_scripts_are_gone(self):
        _response, html = self._page()
        self.assertNotIn("intraoral_segmentation.js", html)
        self.assertNotIn("modality_viewers/intraoral.js", html)
        # And the old sidebar workbench, whose ids the new markup deliberately reuses.
        self.assertNotIn('id="intraoralSegmentationTeethGrid"', html)
        self.assertNotIn("data-segmentation-root", html)

    def test_konva_is_gone(self):
        """Phase 10 removed the last consumer, so the tag goes with it.

        This assertion used to say the opposite, and that is why the tag survived Phases
        5 and 7: each removed a Konva consumer while the laparoscopy annotator was still
        drawing with it, and a tidy-up either time would have been a silent regression on
        a page nobody was looking at. Phase 10 deleted that annotator, so the guard
        inverts rather than being deleted -- what needs preventing now is the tag
        creeping back onto a page with nothing to use it.
        """
        _response, html = self._page()
        self.assertNotIn("konva.min.js", html.lower())
        # The panoramic is on Cornerstone now; asserting its script here would pin a file
        # that no longer exists.
        self.assertNotIn("cbct_panorex_editor.js", html)

    def test_the_photo_stack_entry_is_loaded_once_for_both_surfaces(self):
        _response, html = self._page()
        self.assertEqual(html.count("app/photo-stack.js"), 1)

    def test_a_project_with_segmentation_off_renders_the_viewer_without_the_workbench(self):
        """The gate is the project's, and the page must still show the photographs.

        "Off" means the project configures methods and this is not among them. A project
        that configures *none* is treated as unconfigured and shown everything, which is
        this page's existing convention -- the `ios_landmarks` control beside this one is
        written the same way, and changing it for one control would make the two disagree
        about what an empty list means.
        """
        other, _ = AnnotationMethod.objects.get_or_create(
            slug="voice_caption", defaults={"name": "Voice Captions"}
        )
        self.project.annotation_methods.set([other])
        _response, html = self._page()
        self.assertIn('id="intraoralPhotoViewport"', html)
        self.assertNotIn('id="segTeethGrid"', html)
        self.assertNotIn('id="segMode"', html)

    def test_an_unconfigured_project_is_shown_the_workbench(self):
        # The other half of the convention above, asserted so the two cases cannot be
        # conflated by a later reader.
        self.project.annotation_methods.set([])
        _response, html = self._page()
        self.assertIn('id="segTeethGrid"', html)
