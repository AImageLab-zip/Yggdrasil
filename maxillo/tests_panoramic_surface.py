"""The panoramic surface, as the patient-detail page wires it.

Roadmap Phase 7 replaced 924 lines of Konva (`modality_viewers/cbct_panorex_editor.js`)
with the `panoramic-cpr` bundle entry: the arch is edited on a Cornerstone axial viewport
and the strip reformats live through vtk.js's `ImageCPRMapper`, while the strip that is
*saved* is still baked by `static/js/seg2pano_core.js` so the exports keep their bytes.

Every id below is resolved by `frontend/imaging/panoramic/controls.js`. Phase 5's lesson is
the reason this module exists: **a template id joining two files is an untested
interface**, and a rename leaves the JS holding ``None`` and a control stuck in whatever
state the template shipped -- silently, and only on the surface being replaced.
"""

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from common.models import FileRegistry, Job, Modality, Project, ProjectAccess
from maxillo.models import Folder, Patient

#: The ids `frontend/imaging/panoramic/controls.js` looks up by name.
CONTROL_IDS = (
    "cbctPanorexEditor",
    "panorexEditorStatus",
    "panorexAxialStage",
    "panorexCprStage",
    "panorexResultCanvas",
    "panorexEmptyResult",
    "panorexProgress",
    "panorexProgressBar",
    "panorexEditorError",
    "panorexEditorErrorMessage",
    "panorexRetry",
    "panorexSave",
    "panorexZSlider",
    "panorexZValue",
    "panorexPrevZ",
    "panorexNextZ",
    "panorexResetAuto",
    "editSavedPanoramic",
)


@override_settings(SECURE_SSL_REDIRECT=False)
class PanoramicSurfaceRenderTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.update_or_create(
            slug="maxillo", defaults={"name": "maxillo", "domain": "maxillo"}
        )
        self.modality, _ = Modality.objects.get_or_create(
            slug="cbct", defaults={"name": "CBCT"}
        )
        Modality.objects.get_or_create(slug="panoramic", defaults={"name": "Panoramic"})
        self.project.modalities.set([self.modality])

        self.user = User.objects.create_user(username="pano-render", password="x")  # noqa: S106
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)

        self.folder = Folder.objects.create(name="Arches", project=self.project)
        self.patient = Patient.objects.create(
            patient_id=9901, name="Arch patient", folder=self.folder, project=self.project
        )
        self.patient.modalities.add(self.modality)
        job = Job.objects.create(
            domain="maxillo", patient=self.patient, modality_slug="cbct", status="completed"
        )
        FileRegistry.objects.create(
            patient=self.patient,
            domain="maxillo",
            file_type="cbct_processed",
            subtype="volume_nifti",
            file_path="maxillo/processed/cbct/v.nii.gz",
            file_size=8,
            file_hash="a" * 64,
            modality=self.modality,
            processing_job=job,
        )

    def _render(self):
        response = self.client.get(
            reverse("maxillo:patient_detail", kwargs={"patient_id": self.patient.patient_id})
        )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_every_id_the_surface_resolves_is_rendered(self):
        html = self._render()

        for control_id in CONTROL_IDS:
            with self.subTest(control=control_id):
                self.assertIn(f'id="{control_id}"', html)

    def test_the_cbct_grid_keeps_its_crosshair_and_its_shift_hint(self):
        """The other branch of the shared toolbar, which the brain page takes the false
        side of. `FIXED_CBCT_LAYOUT` is three orthogonal planes, so `supportsCrosshairs`
        is true here and the crosshair holds the plain primary button -- which is why
        window/level is behind Shift on this page and not on the brain one.
        """
        html = self._render()

        self.assertIn('data-ygg-tool="Crosshairs"', html)
        self.assertNotIn('data-ygg-tool="WindowLevel"', html)
        self.assertIn("Shift+drag to adjust brightness", html)
        # And this grid does pin a volume render, so the expand button means something.
        self.assertIn('id="cbctExpand3D"', html)

    def test_the_two_preview_panes_are_both_present_and_both_start_hidden(self):
        """The live reformat and the baked strip share one box, one at a time.

        They are not the same image -- the ray-sum especially -- so the surface shows the
        CPR while the arch is moving and the bake once it has settled. A page that shipped
        with either visible would show a reader an empty canvas or a black viewport before
        anything had been generated.
        """
        html = self._render()

        self.assertIn('id="panorexCprStage"', html)
        self.assertIn('id="panorexResultCanvas"', html)
        for element in ('id="panorexCprStage"', 'id="panorexResultCanvas"'):
            start = html.index(element)
            tag = html[start:html.index(">", start)]
            self.assertIn("hidden", tag)

    def test_the_editor_carries_its_own_csrf_token(self):
        """The save is a multipart POST from a bundle that cannot read the cookie.

        ``CSRF_USE_SESSIONS`` leaves no cookie to read and ``CSRF_COOKIE_HTTPONLY`` would
        block reading one if there were, so the hidden input is the only source that works.
        """
        html = self._render()

        section = html[html.index('id="cbctPanorexEditor"'):]
        section = section[: section.index("</section>")]
        self.assertIn("csrfmiddlewaretoken", section)

    def test_the_section_declares_what_this_user_may_do(self):
        html = self._render()

        section = html[html.index('id="cbctPanorexEditor"'):]
        tag = section[: section.index(">")]
        # Both gates the surface reads before it mounts anything at all.
        self.assertIn("data-can-edit=", tag)
        self.assertIn("data-panoramic-locked=", tag)

    def test_the_replaced_script_is_gone(self):
        html = self._render()

        self.assertNotIn("modality_viewers/cbct_panorex_editor.js", html)

    def test_the_reconstruction_core_is_still_loaded_and_before_the_bundle(self):
        """`seg2pano_core.js` is read through a global, so order is a real dependency.

        It is a classic script and the entry is a deferred module, so the core has executed
        by the time the module runs -- but only if the tag is still on the page. Bundling a
        copy instead would create a second implementation of the arch mathematics, which is
        the one thing decision #8 cannot tolerate.
        """
        html = self._render()

        self.assertIn("js/seg2pano_core.js", html)
        self.assertLess(html.index("js/seg2pano_core.js"), html.index("panoramic-cpr"))

    def test_the_panoramic_entry_is_loaded(self):
        html = self._render()

        self.assertIn("panoramic-cpr", html)

    def test_konva_is_gone(self):
        """Phase 10 removed the last consumer, so the tag goes with it.

        This assertion used to say the opposite. It was guarding against a *premature*
        removal -- Phases 5 and 7 each took a Konva consumer away and the laparoscopy
        annotator was still drawing with it, so a tidy-up here would have been a silent
        regression on a page nobody was looking at. Phase 10 deleted that annotator, so
        the guard inverts rather than being deleted: what needs preventing now is the
        tag creeping back in on a page that has nothing to use it.
        """
        html = self._render()

        self.assertNotIn("konva.min.js", html.lower())
