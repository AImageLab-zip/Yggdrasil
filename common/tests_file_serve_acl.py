"""File-serving authorization is scoped to the file's own domain.

Regression cover for the ACL defect in ``maxillo.api_views.files.serve_file``:
it resolved the patient with an ``if laparoscopy / else .patient`` branch (so a
brain row consulted the maxillo FK) and then authorized *every* domain against
a hardcoded ``Project.objects.filter(slug='maxillo')``, passing the literal
``'maxillo'`` into ``user_is_project_admin``/``user_can_read_folder``. Brain and
laparoscopy files were therefore gated on maxillo project membership in both
directions.

The laparoscopy exposure was the widest: ``laparoscopy/urls.py`` includes
``maxillo.app_urls`` under its own namespace, so laparoscopy files are served by
this very view.

Setup shape follows ``common.tests_raw_data_lock``.
"""
import uuid

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from brain.models import Folder as BrainFolder, Patient as BrainPatient
from common.file_access import authorize_file_read
from common.models import FileRegistry, Project, ProjectAccess
from laparoscopy.models import (
    Folder as LaparoFolder,
    Patient as LaparoPatient,
)
from maxillo.models import Folder, Patient


def _project(label, domain):
    """A project unique to this module.

    Migrations already seed one project per domain, and ``Project.slug`` is
    unique, so tests must not reuse the domain name as a slug.
    """
    suffix = uuid.uuid4().hex[:8]
    return Project.objects.create(
        name=f"acl-{label}-{suffix}",
        slug=f"acl-{label}-{suffix}",
        domain=domain,
    )


def _registry(patient, file_type, domain, **extra):
    fk = {"patient": patient} if domain == "maxillo" else {f"{domain}_patient": patient}
    return FileRegistry.objects.create(
        file_type=file_type,
        file_path=f"{domain}/{file_type}/{uuid.uuid4()}.bin",
        file_size=1,
        file_hash="0" * 64,
        domain=domain,
        **fk,
        **extra,
    )


class FileServeAclTests(TestCase):
    """Cross-domain denial matrix for ``authorize_file_read``."""

    @classmethod
    def setUpTestData(cls):
        cls.maxillo_project = _project("maxillo", "maxillo")
        cls.brain_project = _project("brain", "brain")
        cls.laparo_project = _project("laparoscopy", "laparoscopy")

        cls.maxillo_folder = Folder.objects.create(
            name="mx", project=cls.maxillo_project
        )
        cls.brain_folder = BrainFolder.objects.create(
            name="br", project=cls.brain_project
        )
        cls.laparo_folder = LaparoFolder.objects.create(
            name="lp", project=cls.laparo_project
        )

        cls.maxillo_patient = Patient.objects.create(
            patient_id=9001, folder=cls.maxillo_folder, project=cls.maxillo_project
        )
        cls.brain_patient = BrainPatient.objects.create(
            patient_id=9002, folder=cls.brain_folder, project=cls.brain_project
        )
        cls.laparo_patient = LaparoPatient.objects.create(
            patient_id=9003, folder=cls.laparo_folder, project=cls.laparo_project
        )

        cls.maxillo_file = _registry(cls.maxillo_patient, "cbct_raw", "maxillo")
        cls.brain_file = _registry(cls.brain_patient, "braintumor_mri_t1_raw", "brain")
        cls.laparo_file = _registry(cls.laparo_patient, "video_raw", "laparoscopy")

    def _user(self, name, project, role="viewer"):
        user = User.objects.create_user(username=name, password="pw")  # noqa: S106
        if project is not None:
            ProjectAccess.objects.create(user=user, project=project, role=role)
        return user

    def _allowed(self, user, file_obj, namespace):
        allowed, _, _ = authorize_file_read(user, file_obj, namespace)
        return allowed

    def test_member_reads_own_domain_file(self):
        """The baseline each domain must satisfy."""
        for name, project, file_obj, namespace in (
            ("mx_own", self.maxillo_project, self.maxillo_file, "maxillo"),
            ("br_own", self.brain_project, self.brain_file, "brain"),
            ("lp_own", self.laparo_project, self.laparo_file, "laparoscopy"),
        ):
            with self.subTest(domain=namespace):
                user = self._user(name, project)
                self.assertTrue(self._allowed(user, file_obj, namespace))

    def test_maxillo_member_denied_other_domains(self):
        """The grant half of the defect: maxillo access must not leak across.

        Previously ``user_is_project_admin(user, 'maxillo')`` and
        ``user_can_read_folder(..., 'maxillo')`` were consulted for brain and
        laparoscopy rows, so a maxillo member read both.
        """
        user = self._user("mx_only", self.maxillo_project, role="admin")
        self.assertFalse(self._allowed(user, self.brain_file, "brain"))
        self.assertFalse(self._allowed(user, self.laparo_file, "laparoscopy"))

    def test_laparoscopy_member_reads_laparoscopy_file(self):
        """The denial half: a laparoscopy-only member was refused their own data.

        ``laparoscopy/urls.py`` routes through ``maxillo.app_urls``, so this is
        the path real laparoscopy traffic takes.
        """
        user = self._user("lp_only", self.laparo_project, role="annotator")
        self.assertTrue(self._allowed(user, self.laparo_file, "laparoscopy"))
        self.assertFalse(self._allowed(user, self.maxillo_file, "maxillo"))

    def test_brain_member_reads_brain_file_and_no_other(self):
        user = self._user("br_only", self.brain_project, role="admin")
        self.assertTrue(self._allowed(user, self.brain_file, "brain"))
        self.assertFalse(self._allowed(user, self.maxillo_file, "maxillo"))
        self.assertFalse(self._allowed(user, self.laparo_file, "laparoscopy"))

    def test_brain_file_resolves_brain_patient_not_maxillo_fk(self):
        """The FK-resolution half of the defect, asserted directly."""
        allowed, error, status = authorize_file_read(
            self._user("br_fk", self.brain_project), self.brain_file, "brain"
        )
        self.assertTrue(allowed, msg=error)
        # The maxillo FK on a brain row is empty; resolution must not use it.
        self.assertIsNone(self.brain_file.patient)
        self.assertEqual(self.brain_file.brain_patient_id, self.brain_patient.pk)
        self.assertIsNone(status)

    def test_namespace_does_not_override_the_rows_own_domain(self):
        """A brain row served under the global ``api`` namespace stays brain-scoped."""
        maxillo_user = self._user("mx_api", self.maxillo_project, role="admin")
        self.assertFalse(self._allowed(maxillo_user, self.brain_file, "api"))
        brain_user = self._user("br_api", self.brain_project)
        self.assertTrue(self._allowed(brain_user, self.brain_file, "api"))

    def test_user_without_any_access_is_denied_everywhere(self):
        user = self._user("nobody", None)
        for file_obj, namespace in (
            (self.maxillo_file, "maxillo"),
            (self.brain_file, "brain"),
            (self.laparo_file, "laparoscopy"),
        ):
            with self.subTest(domain=namespace):
                self.assertFalse(self._allowed(user, file_obj, namespace))

    def test_staff_reads_every_domain(self):
        staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True  # noqa: S106
        )
        self.assertTrue(self._allowed(staff, self.maxillo_file, "maxillo"))
        self.assertTrue(self._allowed(staff, self.brain_file, "brain"))
        self.assertTrue(self._allowed(staff, self.laparo_file, "laparoscopy"))

    def test_deleted_patient_is_404_not_403(self):
        self.laparo_patient.deleted = True
        self.laparo_patient.save(update_fields=["deleted"])
        self.addCleanup(
            lambda: LaparoPatient.objects.filter(pk=self.laparo_patient.pk).update(
                deleted=False
            )
        )
        user = self._user("lp_del", self.laparo_project)
        allowed, _, status = authorize_file_read(user, self.laparo_file, "laparoscopy")
        self.assertFalse(allowed)
        self.assertEqual(status, 404)

    def test_orphaned_file_requires_admin_anywhere(self):
        orphan = FileRegistry.objects.create(
            file_type="cbct_raw",
            file_path=f"maxillo/orphan/{uuid.uuid4()}.bin",
            file_size=1,
            file_hash="0" * 64,
            domain="maxillo",
        )
        viewer = self._user("orph_viewer", self.maxillo_project, role="viewer")
        admin = self._user("orph_admin", self.maxillo_project, role="admin")
        self.assertFalse(self._allowed(viewer, orphan, "maxillo"))
        self.assertTrue(self._allowed(admin, orphan, "maxillo"))

    def test_missing_file_is_404(self):
        allowed, _, status = authorize_file_read(
            self._user("nf", self.maxillo_project), None, "maxillo"
        )
        self.assertFalse(allowed)
        self.assertEqual(status, 404)


class FileServeEndpointAclTests(TestCase):
    """The same matrix through the real HTTP endpoints.

    ``authorize_file_read`` being correct is not enough -- the views have to
    call it. Denials short-circuit before any object-storage read, so these
    assert on status codes without needing storage.

    Two routes reach the same view, and only one of them was ever guarded:

    * ``/{domain}/api/processing/files/serve/<id>/`` -- ``ActiveProfileMiddleware``
      redirects to ``/`` when the user has no ``ProjectAccess`` for that
      domain's project, so cross-domain access was already bounced with a 302
      before the view ran.
    * ``/api/processing/files/serve/<id>/`` (the global ``api`` namespace,
      wired at ``yggdrasil/urls.py`` and used by
      ``templates/common/sections/file_management_section.html``) -- the
      middleware only inspects ``maxillo``/``brain``/``laparoscopy`` path
      prefixes, so ``api`` is skipped and the view's own ACL was the *only*
      gate. That is where the hardcoded ``slug='maxillo'`` check was actually
      reachable, and it is the route these tests care about most.
    """

    @classmethod
    def setUpTestData(cls):
        # The defect authorized every domain against
        # ``Project.objects.filter(slug='maxillo')``, so reproducing it
        # faithfully requires the maxillo user to hold *that* project -- the
        # one migrations seed -- exactly as a real maxillo member does.
        cls.maxillo_project = Project.objects.filter(slug="maxillo").first() or (
            Project.objects.create(name="Maxillo", slug="maxillo", domain="maxillo")
        )
        cls.brain_project = _project("ep-brain", "brain")
        cls.laparo_project = _project("ep-laparoscopy", "laparoscopy")

        cls.maxillo_patient = Patient.objects.create(
            patient_id=9101,
            folder=Folder.objects.create(name="mx-ep", project=cls.maxillo_project),
            project=cls.maxillo_project,
        )
        cls.brain_patient = BrainPatient.objects.create(
            patient_id=9102,
            folder=BrainFolder.objects.create(name="br-ep", project=cls.brain_project),
            project=cls.brain_project,
        )
        cls.laparo_patient = LaparoPatient.objects.create(
            patient_id=9103,
            folder=LaparoFolder.objects.create(
                name="lp-ep", project=cls.laparo_project
            ),
            project=cls.laparo_project,
        )

        cls.maxillo_file = _registry(cls.maxillo_patient, "cbct_raw", "maxillo")
        cls.brain_file = _registry(cls.brain_patient, "braintumor_mri_t1_raw", "brain")
        cls.laparo_file = _registry(cls.laparo_patient, "video_raw", "laparoscopy")

    def _login(self, name, project, role="viewer"):
        user = User.objects.create_user(username=name, password="pw")  # noqa: S106
        if project is not None:
            ProjectAccess.objects.create(user=user, project=project, role=role)
        self.client.force_login(user)
        return user

    def _global(self, file_obj):
        return reverse("api:api_serve_file", kwargs={"file_id": file_obj.id})

    def test_global_route_denies_maxillo_member_a_laparoscopy_file(self):
        """The unguarded route: no middleware bounce, so the view must deny."""
        self._login("ep_g_mx", self.maxillo_project, role="admin")
        response = self.client.get(self._global(self.laparo_file))
        self.assertEqual(response.status_code, 403)

    def test_global_route_denies_maxillo_member_a_brain_file(self):
        self._login("ep_g_mx2", self.maxillo_project, role="admin")
        response = self.client.get(self._global(self.brain_file))
        self.assertEqual(response.status_code, 403)

    def test_global_route_denies_laparoscopy_member_a_maxillo_file(self):
        self._login("ep_g_lp", self.laparo_project, role="admin")
        response = self.client.get(self._global(self.maxillo_file))
        self.assertEqual(response.status_code, 403)

    def test_global_route_allows_owner(self):
        """Authorization passes, so the view proceeds to the storage read.

        Object storage is unavailable under test, so any non-403 proves the ACL
        let this user through -- the half of the defect that wrongly denied
        laparoscopy-only members their own data.
        """
        self._login("ep_g_lp_own", self.laparo_project, role="annotator")
        response = self.client.get(self._global(self.laparo_file))
        self.assertNotEqual(response.status_code, 403)

    def test_global_route_denies_stranger(self):
        self._login("ep_g_nobody", None)
        self.assertEqual(
            self.client.get(self._global(self.maxillo_file)).status_code, 403
        )

    def test_namespaced_route_bounces_cross_domain_at_middleware(self):
        """Documents the pre-existing coarse gate, so a change to it is noticed."""
        self._login("ep_ns_mx", self.maxillo_project, role="admin")
        response = self.client.get(
            reverse(
                "laparoscopy:api_serve_file", kwargs={"file_id": self.laparo_file.id}
            )
        )
        self.assertEqual(response.status_code, 302)

    def test_brain_route_denies_maxillo_member(self):
        self._login("ep_br_mx", self.maxillo_project, role="admin")
        response = self.client.get(
            reverse("brain:api_serve_file", kwargs={"file_id": self.brain_file.id})
        )
        self.assertIn(response.status_code, (302, 403))

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(self._global(self.maxillo_file))
        self.assertIn(response.status_code, (301, 302))


class FilenameSuffixedServeRouteTests(TestCase):
    """The ``.../serve/<id>/<filename>`` form added for Cornerstone (F3).

    It reaches the *same* view, so the ACL is inherited rather than reimplemented --
    but finding F9 is precisely that a new endpoint must not *assume* it is covered:
    ``ActiveProfileMiddleware`` only inspects the ``maxillo``/``brain``/``laparoscopy``
    path prefixes, so on the global ``api`` namespace the view's own check is the only
    gate there is. So it is asserted, in every namespace, rather than reasoned about.

    Also pinned here: the shape of the URL. The suffix has to be the final path
    segment with no trailing slash, or ``new URL(url).pathname.endsWith('.gz')`` in
    ``createNiftiImageIdsAndCacheMetadata.js`` goes false and a gzipped volume is
    handed to the parser still compressed.
    """

    @classmethod
    def setUpTestData(cls):
        cls.maxillo_project = Project.objects.filter(slug="maxillo").first() or (
            Project.objects.create(name="Maxillo", slug="maxillo", domain="maxillo")
        )
        cls.brain_project = _project("fn-brain", "brain")
        cls.laparo_project = _project("fn-laparoscopy", "laparoscopy")

        cls.maxillo_patient = Patient.objects.create(
            patient_id=9201,
            folder=Folder.objects.create(name="mx-fn", project=cls.maxillo_project),
            project=cls.maxillo_project,
        )
        cls.laparo_patient = LaparoPatient.objects.create(
            patient_id=9202,
            folder=LaparoFolder.objects.create(name="lp-fn", project=cls.laparo_project),
            project=cls.laparo_project,
        )
        cls.maxillo_file = _registry(cls.maxillo_patient, "cbct_raw", "maxillo")
        cls.laparo_file = _registry(cls.laparo_patient, "video_raw", "laparoscopy")

    def _login(self, name, project, role="viewer"):
        user = User.objects.create_user(username=name, password="pw")  # noqa: S106
        if project is not None:
            ProjectAccess.objects.create(user=user, project=project, role=role)
        self.client.force_login(user)
        return user

    def _named(self, namespace, file_obj, filename="volume.nii.gz"):
        return reverse(
            f"{namespace}:api_serve_file_named",
            kwargs={"file_id": file_obj.id, "filename": filename},
        )

    # -- URL shape ---------------------------------------------------------

    def test_the_url_ends_with_the_filename_and_no_trailing_slash(self):
        url = self._named("api", self.maxillo_file)
        self.assertTrue(url.endswith("/volume.nii.gz"), url)
        self.assertFalse(url.endswith("/"), url)

    def test_the_route_is_registered_in_all_four_namespaces(self):
        # laparoscopy has no urlconf of its own: laparoscopy/urls.py includes
        # maxillo.app_urls under namespace='laparoscopy'. If that ever changes, the
        # route silently disappears for the surface that migrates last.
        for namespace in ("api", "maxillo", "brain", "laparoscopy"):
            with self.subTest(namespace=namespace):
                self.assertTrue(self._named(namespace, self.maxillo_file))

    def test_a_gz_suffix_survives_alongside_a_file_key_query_parameter(self):
        # `file_key` must stay a query parameter: as a path segment it would come
        # after the filename and break `.gz` detection all over again.
        url = self._named("api", self.maxillo_file) + "?file_key=segmentation_nifti"
        path, _, query = url.partition("?")
        self.assertTrue(path.endswith(".gz"))
        self.assertEqual(query, "file_key=segmentation_nifti")

    # -- ACL, inherited but asserted ---------------------------------------

    def test_global_route_denies_cross_domain(self):
        self._login("fn_g_mx", self.maxillo_project, role="admin")
        response = self.client.get(self._named("api", self.laparo_file))
        self.assertEqual(response.status_code, 403)

    def test_global_route_denies_stranger(self):
        self._login("fn_g_nobody", None)
        response = self.client.get(self._named("api", self.maxillo_file))
        self.assertEqual(response.status_code, 403)

    def test_global_route_allows_owner(self):
        # Object storage may be unavailable here; any non-403 proves the ACL passed.
        self._login("fn_g_own", self.maxillo_project, role="annotator")
        response = self.client.get(self._named("api", self.maxillo_file))
        self.assertNotEqual(response.status_code, 403)

    def test_namespaced_route_still_bounces_cross_domain(self):
        self._login("fn_ns_mx", self.maxillo_project, role="admin")
        response = self.client.get(self._named("laparoscopy", self.laparo_file))
        self.assertEqual(response.status_code, 302)

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(self._named("api", self.maxillo_file))
        self.assertIn(response.status_code, (301, 302))

    def test_the_filename_segment_cannot_redirect_the_read(self):
        """A crafted suffix must not change which bytes are served."""
        self._login("fn_g_traverse", self.maxillo_project, role="annotator")
        honest = self.client.get(self._named("api", self.maxillo_file))
        crafted = self.client.get(
            self._named("api", self.maxillo_file, filename="..%2F..%2Fsecret.nii.gz")
        )
        # Same authorization outcome either way: the segment is never read, so it
        # cannot select a different file. (Both may fail later on storage.)
        self.assertEqual(honest.status_code, crafted.status_code)
        self.assertNotEqual(crafted.status_code, 403)
