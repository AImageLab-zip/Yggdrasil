"""The brain volume grid, as the patient-detail page wires it.

Two capabilities came back here after `c03afa6` ("wire the Cornerstone grid, delete
NiiVue and viewer_grid.js") deleted the JavaScript that read them while leaving both
the markup and the server payload in place:

  * **The segmentation overlay.** `viewer_grid_data.segmentationFile` has been emitted
    by this view throughout and read by nobody. The SEG button is bound to it again.
  * **Drag-and-drop.** The chips have carried `draggable="true"` the whole time. With
    nothing bound, one arbitrarily-chosen series was loaded into all four windows and
    there was no way to change any of them.

Phase 5's lesson is why this module exists: a template id or a payload key joining two
files is an untested interface, and a rename leaves the JS holding `None` and a control
stuck in whatever state the template shipped.
"""

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from brain.models import Folder, Patient
from common.models import FileRegistry, Modality, Project, ProjectAccess

#: The id `SEGMENTATION_IDS` in frontend/imaging/grid/bootstrap.js looks up. The
#: per-class list that briefly sat beside it is gone: the overlay is all or nothing.
SEGMENTATION_IDS = ("viewerSegToggle",)

#: `CONTROL_IDS` in frontend/imaging/grid/controls.js. The brain page carried none of
#: these: no crosshair, no measurement tools, no save, although the grid builds both
#: tool groups for every surface and `bindControls` binds whatever it finds.
CONTROL_IDS = (
    "resetCBCTView",
    "cbctAnnotationMode",
    "cbctAnnotationTools",
    "cbctRenderStatus",
)

#: The Cornerstone tool names the toolbar's `data-ygg-tool` buttons name.
#: The measurement tools, which this page simply never had. **Not `Crosshairs`** -- see
#: `test_the_brain_grid_offers_no_crosshair_because_its_planes_are_parallel`.
TOOL_NAMES = (
    "Length",
    "Angle",
    "Bidirectional",
    "Probe",
    "EllipticalROI",
    "RectangleROI",
)


@override_settings(SECURE_SSL_REDIRECT=False)
class BrainVolumeGridSurfaceTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.update_or_create(
            slug="brain-grid", defaults={"name": "Brain grid", "domain": "brain"}
        )
        self.flair, _ = Modality.objects.get_or_create(
            slug="braintumor-mri-flair",
            defaults={"name": "Brain MRI FLAIR", "label": "FLAIR", "domain": "brain"},
        )
        self.seg, _ = Modality.objects.get_or_create(
            slug="braintumor-mri-seg",
            defaults={"name": "Brain MRI Segmentation", "label": "SEG", "domain": "brain"},
        )
        self.project.modalities.set([self.flair, self.seg])

        self.user = User.objects.create_user(username="brain-grid", password="x")  # noqa: S106
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)

        self.folder = Folder.objects.create(name="Studies", project=self.project)
        self.patient = Patient.objects.create(
            name="Study", folder=self.folder, project=self.project
        )
        self.patient.modalities.set([self.flair])

    def _file(self, modality, file_type):
        return FileRegistry.objects.create(
            domain="brain",
            brain_patient=self.patient,
            file_type=file_type,
            file_path=f"brain/{self.patient.patient_id}/{modality.slug}.nii.gz",
            file_size=1024,
            file_hash=f"h-{modality.slug}",
            modality=modality,
        )

    def _page(self):
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()
        response = self.client.get(
            reverse("brain:patient_detail", kwargs={"patient_id": self.patient.patient_id})
        )
        return response, response.content.decode()

    def test_the_page_states_that_windows_are_filled_by_dragging(self):
        """`enableDragDrop` decides both that the chips are bound and that the four
        windows start empty. It has to be stated, not inferred from a client default."""
        self._file(self.flair, "braintumor_mri_flair_processed")
        response, _html = self._page()
        self.assertIs(response.context["viewer_grid_data"]["enableDragDrop"], True)

    def test_the_segmentation_file_is_named_for_the_overlay_to_fetch(self):
        self._file(self.flair, "braintumor_mri_flair_processed")
        self.patient.modalities.add(self.seg)
        seg_row = self._file(self.seg, "braintumor_mri_seg_processed")

        response, _html = self._page()
        payload = response.context["viewer_grid_data"]
        self.assertEqual(payload["segmentationFile"]["id"], seg_row.id)
        # ...and it is not offered as a series to drop into a window: it is a labelmap
        # over whichever series is there.
        self.assertNotIn("braintumor-mri-seg", payload["modalityFiles"])

    def test_the_seg_control_is_rendered_when_there_is_a_segmentation(self):
        self._file(self.flair, "braintumor_mri_flair_processed")
        self.patient.modalities.add(self.seg)
        self._file(self.seg, "braintumor_mri_seg_processed")

        _response, html = self._page()
        for element_id in SEGMENTATION_IDS:
            self.assertIn(f'id="{element_id}"', html, element_id)

    def test_the_seg_control_is_absent_when_there_is_no_segmentation(self):
        """A button that cannot do anything is worse than no button."""
        self._file(self.flair, "braintumor_mri_flair_processed")
        _response, html = self._page()
        for element_id in SEGMENTATION_IDS:
            self.assertNotIn(f'id="{element_id}"', html, element_id)

    def test_the_chips_are_draggable_and_every_window_offers_a_drop_target(self):
        self._file(self.flair, "braintumor_mri_flair_processed")
        _response, html = self._page()
        self.assertIn('draggable="true"', html)
        self.assertIn('data-modality="braintumor-mri-flair"', html)
        self.assertEqual(html.count('class="drop-hint"'), 4)

    def test_the_brain_grid_has_the_same_toolbar_as_the_cbct_grid(self):
        """The measurement tools and the save, which this page simply never had."""
        self._file(self.flair, "braintumor_mri_flair_processed")
        _response, html = self._page()
        for element_id in CONTROL_IDS:
            self.assertIn(f'id="{element_id}"', html, element_id)
        for tool in TOOL_NAMES:
            self.assertIn(f'data-ygg-tool="{tool}"', html, tool)

    def test_the_brain_grid_offers_no_crosshair_because_its_planes_are_parallel(self):
        """`FREE_LAYOUT` is four axial windows, and a crosshair draws the intersection
        lines of the *other* windows' planes: four parallel planes intersect nowhere, so
        `_calculateToolCenterFromAbsoluteCameras` returns null and every click on the
        image is a no-op. `supportsCrosshairs` decides this once and the toolbar follows,
        because a button that looks pressed and does nothing is the defect, not the fix.
        """
        self._file(self.flair, "braintumor_mri_flair_processed")
        _response, html = self._page()
        self.assertNotIn('data-ygg-tool="Crosshairs"', html)
        # The left button is not left unbound: window/level takes it, and the hint drops
        # the Shift it would otherwise be telling the reader to hold for nothing.
        self.assertIn('data-ygg-tool="WindowLevel"', html)
        self.assertIn("Drag to adjust brightness", html)
        self.assertNotIn("Shift+drag to adjust brightness", html)

    def test_the_save_and_clear_buttons_follow_the_write_permission(self):
        self._file(self.flair, "braintumor_mri_flair_processed")
        _response, html = self._page()
        self.assertIn('id="cbctSaveMeasurements"', html)
        self.assertIn("csrfmiddlewaretoken", html)

    def test_the_3d_expand_button_is_not_offered_where_there_is_no_3d_window(self):
        """The brain layout is four freely-assigned windows and pins no volume render,
        so the button would act on nothing."""
        self._file(self.flair, "braintumor_mri_flair_processed")
        _response, html = self._page()
        self.assertNotIn('id="cbctExpand3D"', html)

    def test_the_segmentation_control_is_a_switch_like_annotation_mode(self):
        self._file(self.flair, "braintumor_mri_flair_processed")
        self.patient.modalities.add(self.seg)
        self._file(self.seg, "braintumor_mri_seg_processed")

        _response, html = self._page()
        toggle = html[html.index('id="viewerSegToggle"') :][:400]
        self.assertIn('role="switch"', toggle)
        self.assertIn('aria-checked="false"', toggle)

    def test_the_measurements_endpoints_this_toolbar_saves_through_exist(self):
        """The save button is only honest if the route behind it is registered."""
        from django.urls import reverse

        for name in ("brain:api_save_measurements", "brain:api_measurements_state"):
            self.assertTrue(
                reverse(name, kwargs={"patient_id": self.patient.patient_id})
            )
