"""The orphan-folder repair: folders always belong to a project.

``Folder.project`` was nullable so the folder->project migration could backfill
it, and it stayed nullable afterwards -- which left the field optional in every
ModelForm, the Django admin's "add folder" page included. A project-less folder is
invisible to every project-scoped listing (patient list, upload, export) while
still being reachable by id.

Migration ``0028_folder_project_required`` adopts any remaining orphan into the
domain's catch-all project as "Uncategorized" and makes the column NOT NULL.

The repair itself is exercised by really migrating backwards, planting orphans
through the historical model, and migrating forwards again -- the constraint means
an orphan can no longer be manufactured any other way.
"""
import importlib

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from common.models import Project
from maxillo.models import Folder, Patient

# The migration is self-contained (see its docstring), so its helpers are loaded
# by module path rather than imported from shared code.
_migration = importlib.import_module("maxillo.migrations.0028_folder_project_required")
catchall_project = _migration._catchall_project
unique_name = _migration._unique_name

MIGRATE_FROM = [("maxillo", "0027_maxilloproject")]
MIGRATE_TO = [("maxillo", "0028_folder_project_required")]


class OrphanFolderRepairMigrationTests(TransactionTestCase):
    """Plant orphans at the pre-migration state, then migrate over them."""

    # The migration touches folders, patients and projects.
    available_apps = None

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        executor.loader.build_graph()
        return executor.loader.project_state(targets).apps

    def setUp(self):
        old_apps = self._migrate(MIGRATE_FROM)
        Folder = old_apps.get_model("maxillo", "Folder")
        Patient = old_apps.get_model("maxillo", "Patient")
        HistoricalProject = old_apps.get_model("common", "Project")

        self.catchall, _ = HistoricalProject.objects.get_or_create(
            slug="maxillo", defaults={"name": "Maxillo", "domain": "maxillo"}
        )
        # A folder the project already has, so the rename has to dodge it.
        self.taken = Folder.objects.create(name="Uncategorized", project=self.catchall)
        self.parent = Folder.objects.create(name="Parent", project=self.catchall)

        # Three orphans: the production case (an empty duplicate named after the
        # project), one holding a patient, and one nested under a real folder.
        self.orphan = Folder.objects.create(name="tf4_testset", project=None)
        self.orphan_with_patient = Folder.objects.create(name="Leftover", project=None)
        self.nested_orphan = Folder.objects.create(
            name="Nested", project=None, parent=self.parent
        )
        self.patient = Patient.objects.create(
            name="Kept", project=self.catchall, folder=self.orphan_with_patient
        )

    def tearDown(self):
        # Leave the schema at head for whatever runs next.
        self._migrate(MIGRATE_TO)

    def test_orphans_are_adopted_renamed_and_flattened(self):
        self._migrate(MIGRATE_TO)

        adopted = Folder.objects.filter(
            id__in=[self.orphan.id, self.orphan_with_patient.id, self.nested_orphan.id]
        ).order_by("id")

        self.assertEqual(len(adopted), 3, "no orphan may be dropped")
        for folder in adopted:
            self.assertEqual(folder.project_id, self.catchall.id)
            self.assertIsNone(folder.parent_id, "orphans are flattened to the root")
            self.assertTrue(folder.name.startswith("Uncategorized"))

        # The existing "Uncategorized" folder is untouched and the three adopted
        # folders get distinct suffixed names.
        self.assertEqual(Folder.objects.get(id=self.taken.id).name, "Uncategorized")
        self.assertEqual(
            sorted(f.name for f in adopted),
            ["Uncategorized (2)", "Uncategorized (3)", "Uncategorized (4)"],
        )

    def test_the_repair_keeps_patients_in_their_folder(self):
        self._migrate(MIGRATE_TO)

        patient = Patient.all_objects.get(pk=self.patient.pk)
        self.assertEqual(patient.folder_id, self.orphan_with_patient.id)

    def test_no_orphan_survives_the_migration(self):
        self._migrate(MIGRATE_TO)

        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM maxillo_folder WHERE project_id IS NULL")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_the_column_is_not_null_afterwards(self):
        self._migrate(MIGRATE_TO)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Folder.objects.create(name="No project")


class CatchallProjectResolutionTests(TestCase):
    """Which project adopts an orphan (the migration's ``_catchall_project``)."""

    def _catchall(self):
        project, _ = Project.objects.get_or_create(
            slug="maxillo", defaults={"name": "Maxillo", "domain": "maxillo"}
        )
        return project

    def test_the_domain_catchall_wins(self):
        catchall = self._catchall()
        Project.objects.create(name="Older", slug="older", domain="maxillo")

        self.assertEqual(catchall_project(Project), catchall)

    def test_without_a_catchall_the_oldest_project_of_the_domain_is_used(self):
        Project.objects.filter(slug="maxillo").delete()
        oldest = Project.objects.create(name="First", slug="first", domain="maxillo")
        Project.objects.create(name="Second", slug="second", domain="maxillo")
        # Another domain's project must never be chosen.
        Project.objects.create(name="Brainy", slug="brainy", domain="brain")

        self.assertEqual(catchall_project(Project), oldest)

    def test_with_no_project_at_all_a_catchall_is_created(self):
        Project.objects.filter(domain="maxillo").delete()
        Project.objects.create(name="Brainy", slug="brainy", domain="brain")

        resolved = catchall_project(Project)

        self.assertEqual(resolved.slug, "maxillo")
        self.assertEqual(resolved.domain, "maxillo")


class UniqueNameTests(TestCase):
    def test_suffixes_only_when_needed(self):
        project = Project.objects.create(name="Pu", slug="pu", domain="maxillo")

        self.assertEqual(unique_name(Folder, project, "Uncategorized"), "Uncategorized")
        Folder.objects.create(name="Uncategorized", project=project)
        self.assertEqual(unique_name(Folder, project, "Uncategorized"), "Uncategorized (2)")

    def test_a_same_named_folder_in_another_project_does_not_count(self):
        project = Project.objects.create(name="Pu", slug="pu", domain="maxillo")
        other = Project.objects.create(name="Ou", slug="ou", domain="maxillo")
        Folder.objects.create(name="Uncategorized", project=other)

        self.assertEqual(unique_name(Folder, project, "Uncategorized"), "Uncategorized")


class ProjectIsRequiredTests(TestCase):
    def test_a_folder_cannot_be_saved_without_a_project(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Folder.objects.create(name="No project")

    def test_the_field_is_required_in_forms_too(self):
        # blank=False is what makes the Django admin's add-folder page demand it,
        # which is how the production orphan was created in the first place.
        field = Folder._meta.get_field("project")
        self.assertFalse(field.null)
        self.assertFalse(field.blank)


PATIENT_MIGRATE_FROM = [("maxillo", "0028_folder_project_required")]
PATIENT_MIGRATE_TO = [("maxillo", "0029_patient_project_required")]


class OrphanPatientRepairMigrationTests(TransactionTestCase):
    """``0029_patient_project_required``, exercised by really migrating.

    Patients are planted at the pre-migration state because the constraint the
    migration adds makes a project-less patient impossible afterwards.
    """

    available_apps = None

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        executor.loader.build_graph()
        return executor.loader.project_state(targets).apps

    def setUp(self):
        old_apps = self._migrate(PATIENT_MIGRATE_FROM)
        Folder = old_apps.get_model("maxillo", "Folder")
        Patient = old_apps.get_model("maxillo", "Patient")
        HistoricalProject = old_apps.get_model("common", "Project")

        self.catchall, _ = HistoricalProject.objects.get_or_create(
            slug="maxillo", defaults={"name": "Maxillo", "domain": "maxillo"}
        )
        # A second project whose folder must win over the catch-all.
        self.other = HistoricalProject.objects.create(
            name="Other", slug="other-orphan-patients", domain="maxillo"
        )
        self.other_folder = Folder.objects.create(name="Cases", project=self.other)

        self.filed = Patient.objects.create(
            name="Filed", project=None, folder=self.other_folder
        )
        self.homeless = Patient.objects.create(name="Homeless", project=None, folder=None)
        self.soft_deleted = Patient.objects.create(
            name="Deleted", project=None, folder=None, deleted=True
        )

    def tearDown(self):
        self._migrate(PATIENT_MIGRATE_TO)

    def test_a_patient_with_a_folder_takes_that_folders_project(self):
        self._migrate(PATIENT_MIGRATE_TO)

        patient = Patient.all_objects.get(pk=self.filed.pk)
        self.assertEqual(patient.project_id, self.other.id)
        self.assertEqual(patient.folder_id, self.other_folder.id)

    def test_a_homeless_patient_lands_in_the_catchall_uncategorized_folder(self):
        self._migrate(PATIENT_MIGRATE_TO)

        patient = Patient.all_objects.get(pk=self.homeless.pk)
        self.assertEqual(patient.project_id, self.catchall.id)
        self.assertEqual(patient.folder.name, "Uncategorized")
        self.assertEqual(patient.folder.project_id, self.catchall.id)

    def test_a_soft_deleted_orphan_is_repaired_too(self):
        # The patient default manager hides soft-deleted rows; missing them would
        # leave the NOT NULL constraint unsatisfiable.
        self._migrate(PATIENT_MIGRATE_TO)

        patient = Patient.all_objects.get(pk=self.soft_deleted.pk)
        self.assertEqual(patient.project_id, self.catchall.id)
        self.assertTrue(patient.deleted)

    def test_no_orphan_patient_survives_the_migration(self):
        self._migrate(PATIENT_MIGRATE_TO)

        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM maxillo_patient WHERE project_id IS NULL")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_the_column_is_not_null_afterwards(self):
        self._migrate(PATIENT_MIGRATE_TO)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Patient.objects.create(name="No project")


class PatientProjectIsRequiredTests(TestCase):
    def test_a_patient_cannot_be_saved_without_a_project(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Patient.objects.create(name="No project")

    def test_the_field_is_required_in_forms_too(self):
        field = Patient._meta.get_field("project")
        self.assertFalse(field.null)
        self.assertFalse(field.blank)

    def test_folder_stays_optional_because_its_on_delete_nulls_it(self):
        # Folder deletion is SET_NULL, so a patient legitimately ends up
        # folder-less; only `project` is mandatory.
        field = Patient._meta.get_field("folder")
        self.assertTrue(field.null)
