"""The IOS mesh surface, as the patient-detail page wires it.

Roadmap Phase 6 replaced `static/js/modality_viewers/ios.js` (1539 lines) with a
Cornerstone entry, and took the three Three.js r128 CDN tags in `base.html` with it --
that file was their only consumer.

The wiring is only ever exercised by rendering the page. A missing context variable, a
payload under the wrong element id, or a renamed control would leave the tab blank in a
browser and green in CI. That is not hypothetical: Phase 5's browser check found the tooth
grid permanently invisible because a CSS class nothing set any more still gated it, and
three of its four defects were a value with two owners where only one was updated.

The element ids asserted here are the interface between this template and
`frontend/imaging/mesh/meshControls.js`. A rename on either side leaves the JS holding
`null` and the control stuck in whatever state the template shipped -- silently.
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
class IosSurfaceRenderTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.update_or_create(
            slug="maxillo", defaults={"name": "maxillo", "domain": "maxillo"}
        )
        self.modality, _ = Modality.objects.get_or_create(
            slug="ios", defaults={"name": "IOS"}
        )
        method, _ = AnnotationMethod.objects.get_or_create(
            slug="ios_landmarks", defaults={"name": "IOS Landmarks"}
        )
        self.project.modalities.set([self.modality])
        self.project.annotation_methods.set([method])

        self.user = User.objects.create_user(username="ios-render", password="x")  # noqa: S106
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)

        self.folder = Folder.objects.create(name="Scans", project=self.project)
        self.patient = Patient.objects.create(
            patient_id=9901, name="Scan patient", folder=self.folder, project=self.project
        )
        self.patient.modalities.add(self.modality)
        for index, jaw in enumerate(("upper", "lower")):
            FileRegistry.objects.create(
                patient=self.patient,
                domain="maxillo",
                file_type=f"ios_raw_{jaw}",
                file_path=f"maxillo/ios_raw/{jaw}.stl",
                file_size=4,
                file_hash=str(index) * 64,
                modality=self.modality,
            )

    def _page(self):
        response = self.client.get(
            reverse("maxillo:patient_detail", kwargs={"patient_id": self.patient.patient_id})
        )
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode("utf-8")

    def test_the_page_carries_the_mesh_landmark_payload(self):
        response, html = self._page()
        self.assertIn('id="meshLandmarkData"', html)

        payload = response.context["mesh_landmark_data"]
        self.assertEqual(payload["patientId"], self.patient.patient_id)
        self.assertEqual(
            payload["meshEndpoint"], f"/maxillo/api/patient/{self.patient.patient_id}/data/"
        )
        self.assertEqual(
            payload["landmarkEndpoint"],
            f"/maxillo/api/patients/{self.patient.patient_id}/ios-landmarks/",
        )
        # An admin may edit; the server refuses regardless, and this only decides whether
        # the workbench offers an action that would then fail.
        self.assertIs(payload["canModify"], True)
        # It has to survive `json_script`, which is what the bootstrap parses.
        self.assertIn(json.dumps(payload["landmarkEndpoint"])[1:-1], html)

    def test_every_control_the_javascript_resolves_is_in_the_template(self):
        """The ids `meshControls.js` declares, asserted against what renders.

        Listed literally rather than imported from the JS, because the point is that the
        two agree: a list derived from one of them could not catch them disagreeing.
        """
        _response, html = self._page()
        for element_id in (
            "scan-viewer",
            "ios-viewer",
            "resetView",
            "toggleWireframe",
            "toggleGrid",
            "showUpper",
            "showLower",
            "viewRight",
            "viewLeft",
            "viewFront",
            "viewUpper",
            "viewLower",
            "toggleLandmarkMode",
            "iosLandmarkWorkbench",
            "iosLandmarkStatus",
            "iosLandmarkTeeth",
            "iosLandmarkTypes",
            "toggleLandmarkVisibility",
            "iosVisualizationMenu",
            "iosLandmarkVisibilityWorkbench",
            "landmarkPlaceTool",
            "landmarkSelectTool",
            "undoLandmark",
            "redoLandmark",
            "deleteLandmark",
            "saveLandmarks",
            "landmarkSizeRange",
            "toggleAxis",
            "toggleWhiteBackground",
        ):
            self.assertIn(f'id="{element_id}"', html, element_id)

    def test_redo_is_offered_beside_undo(self):
        """The legacy tool had undo only, over full-document snapshots.

        The roadmap lists that asymmetry as one of the defects this migration closes, so
        the button existing is part of the deliverable rather than a detail.
        """
        _response, html = self._page()
        self.assertIn('id="redoLandmark"', html)

    def test_the_grid_size_buttons_carry_data_attributes_not_inline_handlers(self):
        """`onclick="IOSViewer.updateGridSize(N)"` reached into a global that is gone.

        Four inline handlers and one `window` global are exactly the "value with two
        owners" shape that produced three of Phase 5's four gate defects.
        """
        _response, html = self._page()
        self.assertNotIn("IOSViewer", html)
        for size in (3, 9, 15, 20):
            self.assertIn(f'data-grid-size="{size}"', html)

    def test_the_surface_carries_its_own_csrf_token(self):
        """`CSRF_USE_SESSIONS` is on, so the hidden input is the only source."""
        _response, html = self._page()
        workbench = html.split('id="iosLandmarkWorkbench"', 1)[1]
        self.assertIn("csrfmiddlewaretoken", workbench.split("</section>", 1)[0])

    def test_the_cornerstone_entry_is_loaded_and_the_old_script_is_not(self):
        _response, html = self._page()
        self.assertIn("mesh-landmarks", html)
        self.assertNotIn("modality_viewers/ios.js", html)

    def test_three_js_is_gone_from_every_page(self):
        """The last of the four frontend stacks the migration set out to remove.

        Asserted on a rendered page rather than by grepping the template, because the tags
        lived in `base.html` and so were on *every* page, including ones that never had a
        3D viewer.
        """
        _response, html = self._page()
        for marker in ("three.min.js", "STLLoader.js", "TrackballControls.js"):
            self.assertNotIn(marker, html, marker)

    def test_the_entry_does_not_load_outside_maxillo(self):
        """`ios.js` was loaded unconditionally, on brain and laparoscopy pages too.

        1539 lines downloaded on a page with no `#scan-viewer` to mount into. The
        Cornerstone entries are all inside the namespace guard and this one joins them.
        """
        content = open("templates/common/patient_detail.html", encoding="utf-8").read()
        # Scoped to the script block: `extra_css` has a maxillo guard of its own, and
        # splitting on the first occurrence lands in it.
        scripts = content.split("{% block extra_js %}", 1)[1]
        guard = scripts.split("{% if ns == 'maxillo' %}", 1)[1].split("{% endif %}", 1)[0]
        self.assertIn("mesh-landmarks", guard)
        self.assertIn("volume-grid", guard, "the guard boundaries moved")

    def test_the_legacy_landmark_endpoint_is_gone(self):
        """Decision #3: the old path is deleted in the same commit, not left behind."""
        from django.urls import NoReverseMatch

        with self.assertRaises(NoReverseMatch):
            reverse(
                "maxillo:patient_ios_landmarks",
                kwargs={"patient_id": self.patient.patient_id},
            )

    def test_the_viewer_controls_sit_outside_the_annotation_gate(self):
        """Reading a study that has landmarks on it is not annotating it.

        Landmark visibility and the visualization menu used to live inside
        `{% if 'ios_landmarks' in allowed_annotations %}`, so a project with landmarks
        switched off could not *see* them either -- and marker size, the axes and the
        background were buried in an annotation workbench a reader never opens.
        """
        # A project with *some* methods configured but not this one. Clearing the set
        # entirely means "unconfigured", which by the page's own convention opens every
        # gate -- so it would not test anything.
        other, _ = AnnotationMethod.objects.get_or_create(
            slug="intraoral_segmentation", defaults={"name": "Intraoral Segmentation"}
        )
        self.project.annotation_methods.set([other])
        _response, html = self._page()
        self.assertIn('id="toggleLandmarkVisibility"', html)
        self.assertIn('id="iosVisualizationMenu"', html)
        self.assertIn('id="toggleWhiteBackground"', html)
        self.assertIn('id="toggleAxis"', html)
        self.assertIn('id="landmarkSizeRange"', html)
        # The annotation half is gated, and stays gated.
        self.assertNotIn('id="toggleLandmarkMode"', html)
        self.assertNotIn('id="saveLandmarks"', html)

    def test_landmark_visibility_and_annotate_are_switches(self):
        """Not icon buttons.

        "Is this eye telling me the state or the action?" is the ambiguity the measurement
        toolbars already solved with `role="switch"` plus a visible on/off word.
        """
        _response, html = self._page()
        for element_id in ("toggleLandmarkVisibility", "toggleLandmarkMode"):
            control = html.split(f'id="{element_id}"', 1)[1].split(">", 1)[0]
            self.assertIn('role="switch"', control, element_id)
            self.assertIn('aria-checked="false"', control, element_id)

    def test_the_workbench_uses_the_intraoral_tooth_selector(self):
        """One FDI selector in the application, not two that name the same teeth."""
        _response, html = self._page()
        grid = html.split('id="iosLandmarkTeeth"', 1)[0].rsplit("<div", 1)[1]
        self.assertIn("intraoral-segmentation-teeth-grid", grid)
