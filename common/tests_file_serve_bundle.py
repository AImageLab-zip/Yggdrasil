"""A bundle member can be addressed without a query string (finding F14).

``maxillo.api_views.files.serve_file`` has always been able to serve one file out of
a multi-file ``cbct_processed`` row, via ``?file_key=``. Cornerstone3D's NIfTI loader
cannot use that form: ``createNiftiImageIdsAndCacheMetadata.js:174`` builds every
slice id as ``nifti:${niftiURL}?frame=${i}`` with a literal ``?``, unconditionally, so
a URL that already carries a query string becomes
``...?file_key=volume_nifti?frame=0`` -- two ``?``, so ``frame`` parses as part of the
``file_key`` value and **every slice resolves to frame 0**.

That is not a corner case. ``_resolved_cbct_viewer_source`` in
``maxillo/views/patient_detail.py`` resolves the maxillo CBCT *display* volume to a
``volume_nifti`` member of exactly such a row, so without a query-free form the
Phase 3 volume grid cannot address the volume it exists to show.

Phase 3 therefore adds ``.../serve/<id>/key/<bundle_key>/<filename>``. What follows
pins the addressing rules; the ACL is covered by ``common.tests_file_serve_acl`` and
is asserted here only where the two interact.

Object storage is patched out throughout: these tests are about which path is
*chosen*, not about the bytes, and ``common.file_access.exists`` returns False without
a live backend, which would make every bundle lookup a 404 for the wrong reason.
"""
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import TestCase
from django.urls import reverse

from common.models import FileRegistry, Project, ProjectAccess
from maxillo.models import Folder, Patient

VOLUME_PATH = "maxillo/cbct_processed/job1/volume.nii.gz"
SEGMENTATION_PATH = "maxillo/cbct_processed/job1/segmentation.nii.gz"
PRIMARY_PATH = "maxillo/cbct_processed/job1/bundle.json"


class BundleServeRouteTests(TestCase):
    """The ``key/<bundle_key>/`` form, and its relationship to ``?file_key=``."""

    @classmethod
    def setUpTestData(cls):
        suffix = uuid.uuid4().hex[:8]
        cls.project = Project.objects.create(
            name=f"bundle-{suffix}", slug=f"bundle-{suffix}", domain="maxillo"
        )
        cls.folder = Folder.objects.create(name="mx", project=cls.project)
        cls.patient = Patient.objects.create(
            patient_id=9101, folder=cls.folder, project=cls.project
        )
        cls.bundle = FileRegistry.objects.create(
            patient=cls.patient,
            file_type="cbct_processed",
            file_path=PRIMARY_PATH,
            file_size=1,
            # The sentinel that marks a row as a bundle rather than a single file.
            file_hash="multi-file",
            domain="maxillo",
            metadata={
                "files": {
                    "volume_nifti": {"path": VOLUME_PATH},
                    "segmentation_nifti": {"path": SEGMENTATION_PATH},
                }
            },
        )

    def setUp(self):
        self.user = User.objects.create_user(
            username=f"u{uuid.uuid4().hex[:8]}", password="pw"  # noqa: S106
        )
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)

    def _serve(self, url):
        """Call the view with storage patched out, returning the path it resolved.

        ``streaming_response`` is replaced rather than mocked loosely so that the
        assertion is on the exact key the view decided to stream.
        """
        served = {}

        def fake_stream(path_or_key, **kwargs):
            served["path"] = path_or_key
            return HttpResponse(b"", content_type=kwargs.get("content_type") or "")

        with patch("maxillo.api_views.files.artifact_exists", return_value=True), patch(
            "maxillo.api_views.files.streaming_response", side_effect=fake_stream
        ):
            response = self.client.get(url)
        return response, served.get("path")

    # -- the new form -------------------------------------------------------

    def test_path_borne_key_selects_the_bundle_member(self):
        url = reverse(
            "maxillo:api_serve_file_bundle",
            kwargs={
                "file_id": self.bundle.id,
                "bundle_key": "volume_nifti",
                "filename": "volume.nii.gz",
            },
        )
        # The property the loader depends on, asserted on the generated URL itself.
        self.assertNotIn("?", url)
        self.assertTrue(url.endswith(".gz"))

        response, path = self._serve(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(path, VOLUME_PATH)

    def test_the_segmentation_member_is_reachable_the_same_way(self):
        response, path = self._serve(
            reverse(
                "maxillo:api_serve_file_bundle",
                kwargs={
                    "file_id": self.bundle.id,
                    "bundle_key": "segmentation_nifti",
                    "filename": "seg.nii.gz",
                },
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(path, SEGMENTATION_PATH)

    def test_the_route_is_registered_in_every_serving_namespace(self):
        """maxillo, brain and the global ``api`` prefix all serve files."""
        for namespace, prefix in (
            ("maxillo", "/maxillo/api/"),
            ("brain", "/brain/api/"),
            ("api", "/api/"),
        ):
            with self.subTest(namespace=namespace):
                url = reverse(
                    f"{namespace}:api_serve_file_bundle",
                    kwargs={
                        "file_id": 123,
                        "bundle_key": "volume_nifti",
                        "filename": "v.nii.gz",
                    },
                )
                self.assertEqual(
                    url, f"{prefix}processing/files/serve/123/key/volume_nifti/v.nii.gz"
                )

    def test_filename_stays_decorative_on_the_bundle_route(self):
        """The key resolves the file; the filename only carries the extension.

        Worth pinning: the roadmap floated resolving the bundle key *from* the
        filename, which would have made these two segments fight over the same job.
        """
        response, path = self._serve(
            reverse(
                "maxillo:api_serve_file_bundle",
                kwargs={
                    "file_id": self.bundle.id,
                    "bundle_key": "volume_nifti",
                    # Deliberately naming the *other* member.
                    "filename": "segmentation.nii.gz",
                },
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(path, VOLUME_PATH)

    # -- the old form still works ------------------------------------------

    def test_query_file_key_is_unchanged(self):
        """Existing non-Cornerstone callers keep working, byte for byte."""
        base = reverse(
            "maxillo:api_serve_file", kwargs={"file_id": self.bundle.id}
        )
        response, path = self._serve(f"{base}?file_key=volume_nifti")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(path, VOLUME_PATH)

    def test_no_key_at_all_defaults_to_the_segmentation(self):
        """The pre-existing default for a bundle row, preserved deliberately."""
        response, path = self._serve(
            reverse("maxillo:api_serve_file", kwargs={"file_id": self.bundle.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(path, SEGMENTATION_PATH)

    def test_primary_on_a_bundle_row_is_the_segmentation_default(self):
        """Pre-existing behaviour, pinned because it is not what the word suggests.

        ``serve_file`` treats ``primary`` as "no specific member", which for a bundle
        row means the same ``segmentation_nifti`` default as sending no key at all --
        *not* the row's own ``file_path``. It reads as "the main file", so it is worth
        an assertion rather than a reader's assumption.
        """
        base = reverse("maxillo:api_serve_file", kwargs={"file_id": self.bundle.id})
        response, path = self._serve(f"{base}?file_key=primary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(path, SEGMENTATION_PATH)

    def test_primary_on_a_plain_row_serves_that_rows_own_file(self):
        """Which is where ``primary`` actually appears in practice.

        ``modality_files`` in ``maxillo/views/patient_detail.py`` emits
        ``file_key='primary'`` for every non-CBCT modality, and those are ordinary
        single-file rows: the bundle branch never runs, so the row's own path is
        served. This is what makes it safe for ``volumeUrl`` to map the sentinel onto
        the plain route.
        """
        plain = FileRegistry.objects.create(
            patient=self.patient,
            file_type="cbct_raw",
            file_path="maxillo/cbct_raw/scan.nii.gz",
            file_size=1,
            file_hash="0" * 64,
            domain="maxillo",
        )
        base = reverse("maxillo:api_serve_file", kwargs={"file_id": plain.id})
        response, path = self._serve(f"{base}?file_key=primary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(path, "maxillo/cbct_raw/scan.nii.gz")

    # -- the two forms together --------------------------------------------

    def test_agreeing_keys_are_accepted(self):
        url = reverse(
            "maxillo:api_serve_file_bundle",
            kwargs={
                "file_id": self.bundle.id,
                "bundle_key": "volume_nifti",
                "filename": "v.nii.gz",
            },
        )
        response, path = self._serve(f"{url}?file_key=volume_nifti")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(path, VOLUME_PATH)

    def test_conflicting_keys_are_refused_rather_than_ranked(self):
        """Two different volumes are named; picking one by precedence is a guess.

        The failure this prevents is a viewer that renders the segmentation while
        every label in the UI says it is showing the volume.
        """
        url = reverse(
            "maxillo:api_serve_file_bundle",
            kwargs={
                "file_id": self.bundle.id,
                "bundle_key": "volume_nifti",
                "filename": "v.nii.gz",
            },
        )
        response, path = self._serve(f"{url}?file_key=segmentation_nifti")
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(path, "nothing may be streamed for an ambiguous request")
        self.assertIn("Conflicting bundle keys", response.json()["error"])

    # -- failure modes ------------------------------------------------------

    def test_an_unknown_bundle_key_is_a_404_not_a_silent_fallback(self):
        response, path = self._serve(
            reverse(
                "maxillo:api_serve_file_bundle",
                kwargs={
                    "file_id": self.bundle.id,
                    "bundle_key": "no_such_member",
                    "filename": "v.nii.gz",
                },
            )
        )
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(path)

    def test_the_acl_is_enforced_before_the_bundle_is_resolved(self):
        """A user with no access to the project gets nothing, member or not."""
        outsider = User.objects.create_user(
            username=f"out{uuid.uuid4().hex[:8]}", password="pw"  # noqa: S106
        )
        self.client.force_login(outsider)
        response, path = self._serve(
            reverse(
                "maxillo:api_serve_file_bundle",
                kwargs={
                    "file_id": self.bundle.id,
                    "bundle_key": "volume_nifti",
                    "filename": "v.nii.gz",
                },
            )
        )
        # 302 in practice: ``ActiveProfileMiddleware`` bounces a user with no
        # ProjectAccess to the landing page before the view runs at all. The
        # assertion that matters is the second one -- no bytes were chosen.
        self.assertIn(response.status_code, (302, 403, 404))
        self.assertIsNone(path)

    def test_anonymous_users_are_redirected_to_login(self):
        self.client.logout()
        url = reverse(
            "maxillo:api_serve_file_bundle",
            kwargs={
                "file_id": self.bundle.id,
                "bundle_key": "volume_nifti",
                "filename": "v.nii.gz",
            },
        )
        self.assertEqual(self.client.get(url).status_code, 302)
