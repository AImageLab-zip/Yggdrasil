"""The Phase 3 validation harness page: who may see it, and what it selects.

The comparison logic itself is JavaScript and is covered by ``node --test``
(``frontend/tests/validation.test.js``, 39 cases). What is tested here is the server
half: the access gate, and the corpus the page hands the browser.

The access gate is the part worth being careful about. Finding F10 of
docs/cornerstone-roadmap.md: ``common/demo.py`` ``demo_index`` logs an anonymous
visitor in as a real user, so **every new ``@login_required`` endpoint is instantly
anonymous-public for demo folders**. This page enumerates raw volume URLs across
domains, which is exactly the shape that must not be reachable that way, so it is
staff-only and the demo guest is asserted against directly.
"""
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from common.imaging_validation import VALIDATION_BATCH_LIMIT, candidate_studies
from common.models import FileRegistry, Project, ProjectAccess
from maxillo.models import Folder, Patient


def _project(domain):
    suffix = uuid.uuid4().hex[:8]
    return Project.objects.create(
        name=f"val-{domain}-{suffix}", slug=f"val-{domain}-{suffix}", domain=domain
    )


class ImagingValidationAccessTests(TestCase):
    """Staff only. Not authenticated-only -- see F10."""

    def setUp(self):
        self.url = reverse("imaging_validation")
        self.studies_url = reverse("imaging_validation_studies")

    def _user(self, is_staff=False):
        return User.objects.create_user(
            username=f"u{uuid.uuid4().hex[:8]}",
            password="pw",  # noqa: S106
            is_staff=is_staff,
        )

    def test_anonymous_is_redirected_to_login(self):
        for url in (self.url, self.studies_url):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 302)

    def test_an_ordinary_authenticated_user_is_refused(self):
        self.client.force_login(self._user(is_staff=False))
        for url in (self.url, self.studies_url):
            with self.subTest(url=url):
                # user_passes_test redirects rather than 403s; either way it is not 200.
                self.assertNotEqual(self.client.get(url).status_code, 200)

    def test_a_project_admin_who_is_not_staff_is_still_refused(self):
        """Project admin is the gate ``panoramic_warmup`` uses, and is not enough here.

        This page spans domains and lists raw volume URLs, so membership of one
        project must not open it.
        """
        user = self._user(is_staff=False)
        ProjectAccess.objects.create(user=user, project=_project("maxillo"), role="admin")
        self.client.force_login(user)
        self.assertNotEqual(self.client.get(self.url).status_code, 200)

    def test_staff_reaches_the_page(self):
        self.client.force_login(self._user(is_staff=True))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "common/imaging_validation.html")

    def test_the_demo_guest_cannot_reach_it(self):
        """F10, asserted directly rather than reasoned about.

        ``demo_index`` logs anonymous visitors in as this user. If it were ever given
        staff, or if this page were ever relaxed to ``@login_required``, this test is
        what fails.
        """
        # get_or_create, not create: migrations already seed this user, and the real
        # one is the one worth asserting against.
        guest, _ = User.objects.get_or_create(username=settings.DEMO_GUEST_USERNAME)
        self.assertFalse(guest.is_staff, "the demo guest must never be staff")
        self.client.force_login(guest)
        self.assertNotEqual(self.client.get(self.url).status_code, 200)

    def test_the_page_is_get_only(self):
        self.client.force_login(self._user(is_staff=True))
        self.assertEqual(self.client.post(self.url).status_code, 405)


class ImagingValidationCorpusTests(TestCase):
    """What the page hands the browser to run."""

    @classmethod
    def setUpTestData(cls):
        cls.project = _project("maxillo")
        cls.folder = Folder.objects.create(name="mx", project=cls.project)
        cls.patient = Patient.objects.create(
            patient_id=9201, folder=cls.folder, project=cls.project
        )

    def _registry(self, **kwargs):
        defaults = {
            "patient": self.patient,
            "file_type": "cbct_raw",
            "file_size": 1,
            "file_hash": "0" * 64,
            "domain": "maxillo",
        }
        return FileRegistry.objects.create(**{**defaults, **kwargs})

    def test_a_plain_nifti_row_becomes_one_study_on_the_plain_route(self):
        row = self._registry(file_path="maxillo/cbct_raw/scan.nii.gz")
        studies = candidate_studies("maxillo")
        self.assertEqual(len(studies), 1)
        self.assertEqual(
            studies[0],
            {
                "study": f"maxillo/{row.id}",
                "fileId": row.id,
                "filename": "scan.nii.gz",
                "namespace": "maxillo",
                "bundleKey": "primary",
                "fileType": "cbct_raw",
                "domain": "maxillo",
            },
        )

    def test_a_bundle_row_becomes_one_study_per_member(self):
        """Each member is a separate volume, addressed by the F14 path route.

        The maxillo CBCT *display* volume is a bundle member, so a harness that ran
        only the row's own ``file_path`` would never validate the volume the grid
        actually shows.
        """
        row = self._registry(
            file_type="cbct_processed",
            file_path="maxillo/cbct_processed/job1/bundle.json",
            file_hash="multi-file",
            metadata={
                "files": {
                    "volume_nifti": {"path": "maxillo/cbct_processed/job1/volume.nii.gz"},
                    "segmentation_nifti": {"path": "maxillo/cbct_processed/job1/seg.nii.gz"},
                }
            },
        )
        studies = candidate_studies("maxillo")
        self.assertEqual(len(studies), 2)
        self.assertEqual(
            sorted(study["bundleKey"] for study in studies),
            ["segmentation_nifti", "volume_nifti"],
        )
        volume = next(s for s in studies if s["bundleKey"] == "volume_nifti")
        self.assertEqual(volume["filename"], "volume.nii.gz")
        self.assertEqual(volume["fileId"], row.id)
        self.assertEqual(volume["study"], f"maxillo/{row.id}/volume_nifti")

    def test_bundle_members_that_are_not_nifti_are_skipped(self):
        self._registry(
            file_type="cbct_processed",
            file_path="maxillo/cbct_processed/job2/bundle.json",
            file_hash="multi-file",
            metadata={
                "files": {
                    "volume_nifti": {"path": "maxillo/cbct_processed/job2/volume.nii.gz"},
                    "report_pdf": {"path": "maxillo/cbct_processed/job2/report.pdf"},
                    "preview_png": {"path": "maxillo/cbct_processed/job2/preview.png"},
                }
            },
        )
        studies = candidate_studies("maxillo")
        self.assertEqual([study["bundleKey"] for study in studies], ["volume_nifti"])

    def test_a_non_nifti_plain_row_yields_nothing(self):
        """A zip is not a volume the grid renders, so running it would pad the report."""
        self._registry(file_path="maxillo/cbct_raw/scan.zip")
        self.assertEqual(candidate_studies("maxillo"), [])

    def test_a_row_with_no_path_is_skipped_without_raising(self):
        self._registry(file_path="")
        self.assertEqual(candidate_studies("maxillo"), [])

    def test_the_corpus_is_bounded(self):
        for index in range(VALIDATION_BATCH_LIMIT + 8):
            self._registry(file_path=f"maxillo/cbct_raw/scan{index}.nii.gz")
        self.assertEqual(len(candidate_studies("maxillo")), VALIDATION_BATCH_LIMIT)
        self.assertEqual(len(candidate_studies("maxillo", limit=3)), 3)

    def test_an_unknown_domain_has_no_corpus(self):
        self.assertEqual(candidate_studies("laparoscopy"), [])
        self.assertEqual(candidate_studies(""), [])

    def test_the_studies_endpoint_reports_both_corpora(self):
        self._registry(file_path="maxillo/cbct_raw/scan.nii.gz")
        staff = User.objects.create_user(
            username=f"s{uuid.uuid4().hex[:8]}", password="pw", is_staff=True  # noqa: S106
        )
        self.client.force_login(staff)

        payload = self.client.get(reverse("imaging_validation_studies")).json()
        self.assertIn("maxillo", payload["byDomain"])
        self.assertIn("brain", payload["byDomain"])
        self.assertEqual(len(payload["studies"]), 1)

    def test_the_studies_endpoint_rejects_an_unknown_domain(self):
        staff = User.objects.create_user(
            username=f"s{uuid.uuid4().hex[:8]}", password="pw", is_staff=True  # noqa: S106
        )
        self.client.force_login(staff)
        response = self.client.get(reverse("imaging_validation_studies"), {"domain": "nope"})
        self.assertEqual(response.status_code, 400)

    def test_an_empty_corpus_is_surfaced_on_the_page(self):
        """The gate names both corpora, so an empty one must be visible, not inferred."""
        staff = User.objects.create_user(
            username=f"s{uuid.uuid4().hex[:8]}", password="pw", is_staff=True  # noqa: S106
        )
        self.client.force_login(staff)
        response = self.client.get(reverse("imaging_validation"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("maxillo", response.context["empty_domains"])
        self.assertIn("brain", response.context["empty_domains"])
