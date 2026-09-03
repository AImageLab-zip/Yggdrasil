"""Creating and destroying a project from the Django admin.

Two reported failures, both about the top of the ownership tree:

* **"New project" could not create anything.** ``Project.created_by`` is a
  ``null=True`` audit column that was not ``blank=True``, so the admin's add
  form made it a required picker and rejected the POST with
  ``created_by: This field is required`` -- the control panel links straight to
  that page.
* **"Delete project" silently did nothing.** ``Project`` CASCADEs to patients,
  which CASCADE to ``FileRegistry``, which the annotation graph guards with
  ``PROTECT``. PROTECT raises even when the protecting row is part of the same
  cascade, so the confirmation page became "cannot be deleted" and the POST
  came back 200 with the project still there.

The tests drive the real admin views, because both bugs lived in what the admin
does with the model rather than in the model's own API: ``project.delete()``
raising is correct behavior that :mod:`common.deletion` is licensed to
override, and ``created_by`` was only ever required by a *form*.
"""

from unittest import mock

from django.contrib.auth.models import Permission, User
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase
from django.urls import reverse

from annotations.models import (
    AnnotationPayload,
    AnnotationRevision,
    AnnotationSelector,
    AnnotationSet,
    AnnotationTarget,
    Geometry2DItem,
    SourceResource,
)
from common.models import FileRegistry, Project
from maxillo.admin import FolderAdmin
from maxillo.models import Folder, Patient


class FakeStorage:
    """Records the sweep instead of talking to S3."""

    def __init__(self, keys):
        self._keys = list(keys)
        self.deleted = []

    def list_keys(self, prefix):
        return [key for key in self._keys if key.startswith(prefix)]

    def delete(self, key):
        self.deleted.append(key)


class ProjectAdminCreateTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser("root", "root@example.com", "x")
        self.client.force_login(self.root)

    def test_add_without_created_by_succeeds_and_records_the_author(self):
        response = self.client.post(
            reverse("admin:maxillo_maxilloproject_add"),
            {
                "name": "Fresh", "slug": "fresh", "description": "", "icon": "",
                "is_active": "on",
                # The inlines' management forms; a project is created with no
                # members and no folders in it.
                "access_list-TOTAL_FORMS": "0",
                "access_list-INITIAL_FORMS": "0",
                "access_list-MIN_NUM_FORMS": "0",
                "access_list-MAX_NUM_FORMS": "1000",
                "maxillo_folders-TOTAL_FORMS": "0",
                "maxillo_folders-INITIAL_FORMS": "0",
                "maxillo_folders-MIN_NUM_FORMS": "0",
                "maxillo_folders-MAX_NUM_FORMS": "1000",
            },
        )
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        project = Project.objects.get(name="Fresh")
        # Forced by the admin, not picked in the form: the domain comes from the
        # admin class and the author from the request.
        self.assertEqual(project.domain, "maxillo")
        self.assertEqual(project.created_by, self.root)

    def test_staff_with_the_model_permission_may_not_add_a_project(self):
        """Creating a project is a superuser act, not an ordinary staff one."""
        staff = User.objects.create_user("staff", password="x", is_staff=True)
        staff.user_permissions.add(
            *Permission.objects.filter(
                codename__in=["add_project", "change_project", "delete_project", "view_project"]
            )
        )
        self.client.force_login(staff)

        self.assertEqual(
            self.client.get(reverse("admin:maxillo_maxilloproject_add")).status_code, 403
        )
        project = Project.objects.create(name="Kept", domain="maxillo")
        self.assertEqual(
            self.client.post(
                reverse("admin:maxillo_maxilloproject_delete", args=[project.id]),
                {"post": "yes"},
            ).status_code,
            403,
        )
        self.assertTrue(Project.objects.filter(id=project.id).exists())


class ProjectAdminDeleteTests(TestCase):
    """A project with annotated patients -- the shape that used to be undeletable."""

    def setUp(self):
        self.root = User.objects.create_superuser("root", "root@example.com", "x")
        self.client.force_login(self.root)

        self.project = Project.objects.create(name="Doomed", slug="doomed", domain="maxillo")
        self.other = Project.objects.create(name="Bystander", slug="bystander", domain="maxillo")

        self.folder = Folder.objects.create(name="Cases", project=self.project)
        patient = Patient.objects.create(project=self.project, folder=self.folder)
        # Soft-deleted patients are still rows holding files and annotations.
        soft_deleted = Patient.objects.create(project=self.project, folder=self.folder)
        soft_deleted.deleted = True
        soft_deleted.save(update_fields=["deleted"])

        self.scan = FileRegistry.objects.create(
            file_type="cbct_raw", file_path="doomed/raw/cbct/scan.nii.gz",
            file_size=10, file_hash="h1", domain="maxillo", patient=patient,
        )
        # A bundle row: file_path is the prefix its members live under.
        self.bundle = FileRegistry.objects.create(
            file_type="volume_processed", file_path="doomed/processed/cbct/run1",
            file_size=20, file_hash="h2", domain="maxillo", patient=patient,
            metadata={"files": [{"name": "volume_nifti", "path": "doomed/processed/cbct/run1/vol.nii.gz"}]},
        )
        self.mask = FileRegistry.objects.create(
            file_type="annotation_mask", file_path="doomed/processed/mask.nii.gz",
            file_size=30, file_hash="h3", domain="maxillo", patient=patient,
        )

        resource = SourceResource.objects.create(
            kind="file", identity_key=f"file:{self.scan.id}:", file=self.scan
        )
        annotation_set = AnnotationSet.objects.create(patient=patient, domain="maxillo")
        target = AnnotationTarget.objects.create(
            annotation_set=annotation_set, source_resource=resource
        )
        revision = AnnotationRevision.objects.create(annotation_set=annotation_set, revision_number=1)
        AnnotationPayload.objects.create(revision=revision, file=self.mask)
        # Real geometry, anchored to the target and a selector. This is the shape
        # that made the delete a 500: an item PROTECTs both, and PROTECT raises
        # while the protecting row is there even though the set's own cascade
        # would have taken the item a moment later. A fixture with a bare target
        # and no items cannot see it.
        selector = AnnotationSelector.objects.create(
            target=target, kind="slice", coordinate_system="volume_voxel", slice_index=3
        )
        Geometry2DItem.objects.create(
            revision=revision,
            target=target,
            selector=selector,
            geometry_type="polygon",
            coordinate_system="image_pixel",
            points=[[0, 0], [1, 0], [1, 1]],
            closed=True,
        )

        self.survivor = FileRegistry.objects.create(
            file_type="cbct_raw", file_path="bystander/raw/cbct/scan.nii.gz",
            file_size=40, file_hash="h4", domain="maxillo",
            patient=Patient.objects.create(project=self.other),
        )

        self.storage = FakeStorage(
            [
                "doomed/raw/cbct/scan.nii.gz",
                "doomed/processed/cbct/run1/vol.nii.gz",
                "doomed/processed/cbct/run1/stats.json",
                "doomed/processed/mask.nii.gz",
                "bystander/raw/cbct/scan.nii.gz",
            ]
        )

    def _delete(self):
        with mock.patch(
            "common.object_storage.get_object_storage", return_value=self.storage
        ):
            return self.client.post(
                reverse("admin:maxillo_maxilloproject_delete", args=[self.project.id]),
                {"post": "yes"},
            )

    def test_confirmation_page_states_what_will_be_destroyed(self):
        response = self.client.get(
            reverse("admin:maxillo_maxilloproject_delete", args=[self.project.id])
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # Django's own collector never got this far: it raised on the PROTECTed
        # rows and rendered the dead-end page, which has no confirm button.
        self.assertNotIn("would require deleting the following protected", body)
        self.assertIn('name="post"', body)
        # Real counts, including the soft-deleted patient.
        self.assertIn("2 patients", body)
        self.assertIn("1 annotation payloads", body)
        self.assertIn("destroys annotation work", body)

    def test_delete_takes_the_whole_tree_including_the_annotations(self):
        response = self._delete()
        self.assertEqual(response.status_code, 302)

        self.assertFalse(Project.objects.filter(id=self.project.id).exists())
        self.assertFalse(Folder.objects.filter(id=self.folder.id).exists())
        self.assertFalse(Patient.all_objects.filter(project_id=self.project.id).exists())
        self.assertFalse(
            FileRegistry.objects.filter(
                id__in=[self.scan.id, self.bundle.id, self.mask.id]
            ).exists()
        )
        self.assertFalse(AnnotationSet.objects.exists())
        self.assertFalse(SourceResource.objects.exists())
        self.assertFalse(AnnotationPayload.objects.exists())
        self.assertFalse(AnnotationTarget.objects.exists())
        self.assertFalse(AnnotationSelector.objects.exists())
        self.assertFalse(Geometry2DItem.objects.exists())

    def test_another_project_is_untouched(self):
        self._delete()
        self.assertTrue(Project.objects.filter(id=self.other.id).exists())
        self.assertTrue(FileRegistry.objects.filter(id=self.survivor.id).exists())
        self.assertNotIn("bystander/raw/cbct/scan.nii.gz", self.storage.deleted)

    def test_storage_is_swept_by_the_keys_the_rows_recorded(self):
        self._delete()
        self.assertEqual(
            sorted(self.storage.deleted),
            [
                "doomed/processed/cbct/run1/stats.json",
                "doomed/processed/cbct/run1/vol.nii.gz",
                "doomed/processed/mask.nii.gz",
                "doomed/raw/cbct/scan.nii.gz",
            ],
        )

    def test_an_unreachable_object_store_does_not_fail_a_completed_delete(self):
        with mock.patch(
            "common.object_storage.get_object_storage", side_effect=RuntimeError("no s3")
        ):
            response = self.client.post(
                reverse("admin:maxillo_maxilloproject_delete", args=[self.project.id]),
                {"post": "yes"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Project.objects.filter(id=self.project.id).exists())


class FolderAdminProjectScopeTests(TestCase):
    """A folder belongs to a project *of its own domain*.

    The picker offered every project in the database, so a maxillo folder could
    be filed under a brain project -- invisible to every project-scoped listing
    while pointing across domains. Asserted through the admin's own
    ``formfield_for_foreignkey``, which both renders the widget and validates
    the POST.
    """

    @classmethod
    def setUpTestData(cls):
        cls.maxillo = Project.objects.create(name="Maxillo scope", slug="maxillo-scope", domain="maxillo")
        cls.brain = Project.objects.create(name="Brain scope", slug="brain-scope", domain="brain")

    def _field(self, name):
        admin = FolderAdmin(Folder, AdminSite())
        return admin.formfield_for_foreignkey(
            Folder._meta.get_field(name), RequestFactory().get("/")
        )

    def test_project_picker_offers_only_this_domain(self):
        projects = set(self._field("project").queryset)
        self.assertIn(self.maxillo, projects)
        self.assertNotIn(self.brain, projects)

    def test_a_cross_domain_project_is_refused_on_save(self):
        field = self._field("project")
        with self.assertRaises(Exception):
            field.clean(str(self.brain.id))

    def test_parent_picker_offers_only_this_domain(self):
        """A maxillo folder's parent is a maxillo folder.

        Asserted against the rows this test creates: the migrations seed real
        folders, and asserting over the whole table would test the seed data.
        """
        mine = Folder.objects.create(name="Mine", project=self.maxillo)
        theirs = Folder.objects.create(name="Theirs", project=self.brain)
        offered = set(self._field("parent").queryset)
        self.assertIn(mine, offered)
        self.assertNotIn(theirs, offered)


class ControlPanelProjectLinkTests(TestCase):
    """"New project" is one button per domain.

    A project's domain is fixed at creation and forced by the admin class that
    serves it, so the single hardcoded ``maxilloproject/add/`` link filed every
    project created from the control panel under maxillo, whatever the user
    meant. The targets are reversed, so a renamed proxy fails here rather than
    404-ing for the user.
    """

    def test_one_add_target_per_domain(self):
        from common.domains import DOMAIN_CHOICES, project_admin_add_targets

        targets = project_admin_add_targets()
        self.assertEqual(
            [t["domain"] for t in targets], [slug for slug, _ in DOMAIN_CHOICES]
        )
        for target in targets:
            self.assertEqual(
                target["url"],
                f"/admin/{target['domain']}/{target['domain']}project/add/",
            )

    def test_the_panel_offers_every_domain_to_a_superuser(self):
        root = User.objects.create_superuser("root", "root@example.com", "x")
        self.client.force_login(root)
        body = self.client.get(reverse("admin_control_panel")).content.decode()
        for label in ("New Maxillo project", "New Brain project", "New Laparoscopy project"):
            self.assertIn(label, body)

    def test_staff_who_cannot_create_are_not_offered_the_button(self):
        """The panel is staff-gated; creating a project is not a staff act."""
        staff = User.objects.create_user("staff", password="x", is_staff=True)
        self.client.force_login(staff)
        body = self.client.get(reverse("admin_control_panel")).content.decode()
        self.assertNotIn("New Maxillo project", body)


class ProjectAccessInlineTests(TestCase):
    """Who may use a project is edited on the project's own admin page.

    ``ProjectAccess`` is the only access model authorization reads. Granting it
    used to mean either the app's per-folder dialog -- which promised a
    granularity the system has never had, and in brain wrote to the dead
    ``FolderAccess`` table -- or the standalone changelist, where the project is
    a dropdown of every project in the database.
    """

    def setUp(self):
        self.root = User.objects.create_superuser("root", "root@example.com", "x")
        self.client.force_login(self.root)
        self.project = Project.objects.create(name="Shared", slug="shared", domain="maxillo")
        self.marco = User.objects.create_user("marco", password="x")

    def test_the_access_inline_is_on_the_project_page(self):
        from common.admin import ProjectAccessInline
        from maxillo.admin import MaxilloProjectAdmin
        from maxillo.models import MaxilloProject
        from django.contrib.admin.sites import AdminSite

        admin = MaxilloProjectAdmin(MaxilloProject, AdminSite())
        inlines = admin.get_inlines(RequestFactory().get("/"), self.project)
        # Access first: it is what you come to the page to change.
        self.assertIs(inlines[0], ProjectAccessInline)

    def test_granting_access_from_the_project_page(self):
        from common.models import ProjectAccess

        url = reverse("admin:maxillo_maxilloproject_change", args=[self.project.id])
        response = self.client.post(
            url,
            {
                "name": self.project.name,
                "slug": self.project.slug,
                "description": "",
                "icon": "",
                "is_active": "on",
                "access_list-TOTAL_FORMS": "1",
                "access_list-INITIAL_FORMS": "0",
                "access_list-MIN_NUM_FORMS": "0",
                "access_list-MAX_NUM_FORMS": "1000",
                "access_list-0-project": str(self.project.id),
                "access_list-0-user": str(self.marco.id),
                "access_list-0-role": "viewer",
                "maxillo_folders-TOTAL_FORMS": "0",
                "maxillo_folders-INITIAL_FORMS": "0",
                "maxillo_folders-MIN_NUM_FORMS": "0",
                "maxillo_folders-MAX_NUM_FORMS": "1000",
            },
        )
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        access = ProjectAccess.objects.get(project=self.project, user=self.marco)
        self.assertEqual(access.role, "viewer")

    def test_the_granted_user_can_then_see_the_project_content(self):
        """The grant is the one that authorization actually reads."""
        from common.models import ProjectAccess
        from common.permissions import filter_folders_for_user
        from maxillo.models import Folder

        folder = Folder.objects.create(name="Cases", project=self.project)
        self.assertEqual(
            list(filter_folders_for_user(self.marco, Folder.objects.all(), "maxillo")), []
        )

        ProjectAccess.objects.create(user=self.marco, project=self.project, role="viewer")
        self.assertEqual(
            list(filter_folders_for_user(self.marco, Folder.objects.all(), "maxillo")),
            [folder],
        )


class FolderAccessSurfaceIsGoneTests(TestCase):
    """The per-folder access dialog and its endpoints are removed, not hidden."""

    def test_no_domain_routes_folder_permissions_any_more(self):
        from django.urls import NoReverseMatch

        for domain in ("maxillo", "brain", "laparoscopy"):
            for name in (
                "folder_permissions",
                "upsert_folder_permission",
                "delete_folder_permission",
            ):
                with self.subTest(domain=domain, name=name):
                    with self.assertRaises(NoReverseMatch):
                        reverse(f"{domain}:{name}", args=[1])

    def test_the_permissions_helpers_are_gone_from_the_permission_module(self):
        import common.permissions as permissions

        for name in (
            "get_user_folder_role",
            "user_can_manage_folder_access",
            "_folder_access_model",
        ):
            self.assertFalse(hasattr(permissions, name), name)
