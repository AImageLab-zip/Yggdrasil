"""The CBCT upload surface, now that uploading DICOM is switched off.

Two interfaces are asserted here, and they are the ones that go wrong quietly.
`static/js/cbct_upload.js` resolves the file input **by name** to decide whether a
selection goes through the in-browser converter, so a rename leaves the converter
inert; and the DICOM controls have to be gone from the page at the same time as the
server refuses DICOM, or the page offers an upload that can only fail.
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

    def test_the_one_cbct_control_keeps_the_name_the_js_resolves(self):
        html = self._render()
        self.assertIn('name="cbct"', html)

    def test_the_page_offers_no_dicom_upload(self):
        html = self._render()
        for marker in (
            "DICOM",
            "webkitdirectory",
            'name="cbct_folder_files"',
            'name="cbct_upload_type"',
            'id="cbct_folder_upload"',
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, html)

    def test_the_converter_is_still_loaded_for_the_formats_it_still_handles(self):
        """MetaImage and raw NIfTI still convert in the browser.

        Deleting `cbct_convert.js` outright would be a silent regression on `.mha` and
        on the `.nii.gz` orientation repair that
        `_validate_and_extract_nifti_orientation` demands server-side.
        """
        html = self._render()
        self.assertIn("js/cbct_convert.js", html)
        self.assertIn("js/cbct_upload.js", html)
