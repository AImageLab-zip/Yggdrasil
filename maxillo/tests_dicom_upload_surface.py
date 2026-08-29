"""The CBCT upload surface, now that DICOM is stored rather than converted away.

Phase 8's change is invisible on this page by design -- same two controls, same copy --
which is exactly why it needs a test. The interfaces asserted here are the ones
`static/js/cbct_upload.js` resolves *by name* to decide whether a selection goes
through the in-browser converter, and Phase 5's lesson applies unchanged: **a template
id or an input name joining two files is an untested interface**. A rename leaves the
JS treating a DICOM folder as a NIfTI to convert, which is the exact data loss this
phase exists to end.
"""

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from common.models import Modality, Project, ProjectAccess
from maxillo.models import Folder


@override_settings(SECURE_SSL_REDIRECT=False)
class CbctUploadSurfaceTests(TestCase):
    def setUp(self):
        self.project, _ = Project.objects.update_or_create(
            slug="maxillo", defaults={"name": "maxillo", "domain": "maxillo"}
        )
        modality, _ = Modality.objects.get_or_create(slug="cbct", defaults={"name": "CBCT"})
        self.project.modalities.set([modality])
        Folder.objects.create(name="Uploads", project=self.project)

        self.user = User.objects.create_user(username="dcm-upload", password="x")  # noqa: S106
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)

    def _render(self):
        response = self.client.get(reverse("maxillo:upload_patient"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_both_cbct_controls_are_rendered_with_the_names_the_js_resolves(self):
        html = self._render()

        # cbct_upload.js reads all four: the two radios to switch panes, and the two
        # file inputs by name -- `cbct_folder_files` is how it knows never to convert.
        for marker in (
            'id="cbct_file_upload"',
            'id="cbct_folder_upload"',
            'name="cbct_folder_files"',
            'name="cbct_upload_type"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)

    def test_the_folder_control_still_offers_a_dicom_folder(self):
        # The user-facing promise is unchanged; what changed is that keeping it no
        # longer costs the series.
        html = self._render()
        self.assertIn("DICOM folder", html)
        self.assertIn("webkitdirectory", html)

    def test_the_converter_is_still_loaded_for_the_formats_it_still_handles(self):
        """MetaImage and raw NIfTI still convert in the browser; DICOM does not.

        Deleting `cbct_convert.js` outright would have been a silent regression on
        `.mha` and on the `.nii.gz` orientation repair that
        `_validate_and_extract_nifti_orientation` demands server-side. Only its DICOM
        half is gone.
        """
        html = self._render()
        self.assertIn("js/cbct_convert.js", html)
        self.assertIn("js/cbct_upload.js", html)
