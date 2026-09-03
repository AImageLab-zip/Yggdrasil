"""The admin has to survive production data volume, and say what it is doing.

Two properties are asserted here, both of which the admin failed before this
change:

**A changelist costs the same at 50 rows as at 10.** Query count is measured at
two page sizes and compared. A page whose cost is proportional to the number of
rows on it is a page that works on a developer's fixture and times out on the
37,102-row ``FileRegistry`` table; equality is the only assertion that catches
that, because a fixed overhead of thirty queries is fine and a per-row cost of
one is not.

**The destructive actions state their consequence and ask.** ``retry_failed_jobs``
dispatches one SLURM submission per selected row through
``common.signals._job_post_save``; the first POST must reach the confirmation
page having dispatched nothing.

Plus the invariants the reorganization rests on: ``common``'s models are
registered by ``common``, the ``annotations`` admin cannot write, and the bulk
raw-lock resolver agrees row for row with the per-object predicate it replaces.
"""

import uuid
from unittest import mock

from django.contrib import admin
from django.contrib.auth.models import User
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from annotations.models import AnnotationSet, AnnotationTarget, SourceResource
from common.admin import raw_lock_map
from common.annotation_lock import raw_data_is_locked
from common.models import FileRegistry, Job, Modality, ProcessingStep, Project
from laparoscopy.models import (
    Folder as LaparoFolder,
    Patient as LaparoPatient,
    VoiceCaption as LaparoVoiceCaption,
)
from maxillo.models import Classification, Folder, Patient, VoiceCaption


def _project(slug, domain="maxillo"):
    return Project.objects.create(name=slug, slug=slug, domain=domain)


class AdminQueryBudgetTests(TestCase):
    """Growth in rows must not be growth in queries."""

    #: The two page sizes compared. Both fit on one changelist page (Django's
    #: default is 100), so any difference is per-row work, not pagination.
    SMALL = 10
    LARGE = 50

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_superuser(
            "budget-admin", "budget@example.invalid", "x"
        )
        cls.project = _project("query-budget")
        cls.folder = Folder.objects.create(name="F", project=cls.project)

    def setUp(self):
        self.client.force_login(self.staff)

    def _queries(self, url):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return list(captured)

    def assert_flat(self, url, seed):
        """``seed(n)`` adds n rows; the page must cost the same at 10 and 50."""
        seed(self.SMALL)
        # One warm-up request. The session middleware and the project/profile
        # middlewares write the session on their first pass, which is a
        # per-session cost and not a per-row one; measuring it would compare a
        # cold request against a warm one.
        self._queries(url)
        small = self._queries(url)
        seed(self.LARGE - self.SMALL)
        large = self._queries(url)
        self.assertEqual(
            len(large),
            len(small),
            f"{url} cost {len(small)} queries for {self.SMALL} rows and "
            f"{len(large)} for {self.LARGE}: the changelist scales with the "
            "page, which the production tables cannot afford.\n"
            + "\n".join(q["sql"][:160] for q in large[len(small):][:10]),
        )

    def _patients(self, count):
        return [
            Patient.objects.create(project=self.project, folder=self.folder)
            for _ in range(count)
        ]

    def test_file_registry_changelist_does_not_scale(self):
        """The worst case: 37k rows, four FKs on display and a lock per row.

        Every patient here is unannotated on purpose. That is the expensive
        branch -- a locked patient is answered by the first query and stops,
        an open one is the full union -- so the fixed cost being measured is
        the largest one there is.
        """
        def seed(count):
            for patient in self._patients(count):
                FileRegistry.objects.create(
                    file_type="cbct_raw",
                    file_path=f"maxillo/raw/{uuid.uuid4()}.nii.gz",
                    file_size=2048,
                    file_hash="0" * 64,
                    domain="maxillo",
                    patient=patient,
                )

        self.assert_flat(reverse("admin:common_fileregistry_changelist"), seed)

    def test_patient_changelist_does_not_scale(self):
        self.assert_flat(
            reverse("admin:maxillo_patient_changelist"), self._patients
        )

    def test_laparoscopy_patient_changelist_does_not_scale(self):
        project = _project("query-budget-lap", "laparoscopy")
        folder = LaparoFolder.objects.create(name="F", project=project)

        def seed(count):
            for _ in range(count):
                LaparoPatient.objects.create(project=project, folder=folder)

        self.assert_flat(
            reverse("admin:laparoscopy_patient_changelist"), seed
        )

    def test_annotation_set_changelist_does_not_scale(self):
        def seed(count):
            for patient in self._patients(count):
                annotation_set = AnnotationSet.objects.create(
                    kind="ios_landmarks", domain="maxillo", patient=patient
                )
                resource = SourceResource.objects.create(
                    kind="file", identity_key=str(uuid.uuid4())
                )
                AnnotationTarget.objects.create(
                    annotation_set=annotation_set,
                    source_resource=resource,
                    role="volume",
                )

        self.assert_flat(
            reverse("admin:annotations_annotationset_changelist"), seed
        )

    def test_job_changelist_does_not_scale(self):
        def seed(count):
            with mock.patch("common.signals.celery_app.send_task"):
                for patient in self._patients(count):
                    Job.objects.create(
                        modality_slug="cbct", domain="maxillo", patient=patient
                    )

        self.assert_flat(reverse("admin:common_job_changelist"), seed)


class BulkRawLockTests(TestCase):
    """``raw_lock_map`` is an optimization; it may not be a different answer."""

    def _assert_agrees(self, patients):
        bulk = raw_lock_map(patients)
        for patient in patients:
            self.assertEqual(
                bulk[patient.pk],
                raw_data_is_locked(patient),
                f"bulk and per-object lock disagree on {patient!r}",
            )

    def test_maxillo_every_trigger(self):
        project = _project("bulk-lock")
        folder = Folder.objects.create(name="F", project=project)
        user = User.objects.create_user("bulk-lock-user", password="x")

        def patient():
            return Patient.objects.create(project=project, folder=folder)

        clean = patient()

        by_caption = patient()
        VoiceCaption.objects.create(patient=by_caption, user=user, duration=1.0)

        by_human_classification = patient()
        Classification.objects.create(
            patient=by_human_classification,
            classifier="manual",
            sagittal_left="I",
            sagittal_right="I",
            vertical="normal",
            transverse="normal",
            midline="Unknown",
        )

        # A pipeline row is machine output and must not lock.
        by_pipeline = patient()
        Classification.objects.create(
            patient=by_pipeline,
            classifier="pipeline",
            sagittal_left="I",
            sagittal_right="I",
            vertical="normal",
            transverse="normal",
            midline="Unknown",
        )

        by_annotation_set = patient()
        AnnotationSet.objects.create(
            kind="ios_landmarks",
            domain="maxillo",
            patient=by_annotation_set,
            ever_annotated=True,
        )

        # A prediction: a set exists, but no human work, so it does not lock --
        # and its presence also silences the legacy check for that kind.
        by_prediction = patient()
        AnnotationSet.objects.create(
            kind="ios_landmarks",
            domain="maxillo",
            patient=by_prediction,
            ever_annotated=False,
        )

        patients = [
            clean, by_caption, by_human_classification, by_pipeline,
            by_annotation_set, by_prediction,
        ]
        self._assert_agrees(patients)
        locked = raw_lock_map(patients)
        self.assertFalse(locked[clean.pk])
        self.assertTrue(locked[by_caption.pk])
        self.assertTrue(locked[by_human_classification.pk])
        self.assertFalse(locked[by_pipeline.pk])
        self.assertTrue(locked[by_annotation_set.pk])
        self.assertFalse(locked[by_prediction.pk])

    def test_laparoscopy_and_empty_input(self):
        project = _project("bulk-lock-lap", "laparoscopy")
        folder = LaparoFolder.objects.create(name="F", project=project)
        user = User.objects.create_user("bulk-lock-lap-user", password="x")
        clean = LaparoPatient.objects.create(project=project, folder=folder)
        captioned = LaparoPatient.objects.create(project=project, folder=folder)
        LaparoVoiceCaption.objects.create(
            patient=captioned, user=user, duration=1.0
        )
        self._assert_agrees([clean, captioned])
        self.assertEqual(raw_lock_map([]), {})
        self.assertEqual(raw_lock_map([None]), {})


class AdminStructureTests(TestCase):
    """Where a registration lives, and what the index looks like."""

    def test_common_models_are_registered_by_common(self):
        """The six that used to be registered from ``maxillo/admin.py``.

        Registering them there meant the *Common* section of the admin was
        produced by a domain app: dropping ``maxillo`` from ``INSTALLED_APPS``
        would have silently deleted half of it.
        """
        import common.admin  # noqa: F401  (registers on import)
        import maxillo.admin

        moved = [
            "Modality", "AnnotationMethod", "ProjectAccess", "Job",
            "FileRegistry", "Invitation",
        ]
        registered = {
            model._meta.object_name
            for model in admin.site._registry
            if model._meta.app_label == "common"
        }
        for name in moved:
            self.assertIn(name, registered, f"common.{name} is not in the admin")

        for model, model_admin in admin.site._registry.items():
            if model._meta.app_label != "common":
                continue
            self.assertNotEqual(
                type(model_admin).__module__,
                maxillo.admin.__name__,
                f"common.{model._meta.object_name} is still registered from maxillo",
            )

    def test_the_project_table_itself_is_visible(self):
        """A project whose domain matches no proxy was in no changelist at all."""
        orphan = Project.objects.create(
            name="Orphaned", slug="orphaned", domain="not-a-domain"
        )
        staff = User.objects.create_superuser("struct-admin", "s@example.invalid", "x")
        self.client.force_login(staff)
        response = self.client.get(reverse("admin:common_project_changelist"))
        self.assertContains(response, orphan.name)

    def test_index_is_grouped_by_purpose(self):
        staff = User.objects.create_superuser("index-admin", "i@example.invalid", "x")
        request = RequestFactory().get("/admin/")
        request.user = staff
        headings = [app["name"] for app in admin.site.get_app_list(request)]
        self.assertEqual(
            headings[:6],
            [
                "Projects & access",
                "Clinical data",
                "Annotations",
                "Imaging catalog",
                "Processing",
                "Operations",
            ],
        )
        # Nothing is lost: everything the default index would have shown is
        # still shown, once.
        grouped = [
            (entry["model"]._meta.label)
            for app in admin.site.get_app_list(request)
            for entry in app["models"]
        ]
        self.assertEqual(len(grouped), len(set(grouped)))
        self.assertEqual(len(grouped), len(admin.site._registry))
        self.assertIn("auth.User", grouped)

    def test_the_admin_is_not_called_django_administration(self):
        self.assertEqual(admin.site.site_header, "Yggdrasil administration")
        self.assertTrue(admin.site.index_title)


class AnnotationsAdminIsReadOnlyTests(TestCase):
    """``annotations/services/`` is the only sanctioned writer; the admin looks."""

    def test_every_annotations_admin_refuses_to_write(self):
        request = RequestFactory().get("/admin/")
        request.user = User.objects.create_superuser(
            "ann-admin", "a@example.invalid", "x"
        )
        found = 0
        for model, model_admin in admin.site._registry.items():
            if model._meta.app_label != "annotations":
                continue
            found += 1
            label = model._meta.label
            self.assertFalse(model_admin.has_add_permission(request), label)
            self.assertFalse(model_admin.has_change_permission(request), label)
            self.assertFalse(model_admin.has_delete_permission(request), label)
        self.assertEqual(found, 13, "every annotations model should be registered")

    def test_a_set_can_be_inspected(self):
        project = _project("ann-inspect")
        folder = Folder.objects.create(name="F", project=project)
        patient = Patient.objects.create(project=project, folder=folder)
        annotation_set = AnnotationSet.objects.create(
            kind="ios_landmarks", domain="maxillo", patient=patient,
            ever_annotated=True,
        )
        staff = User.objects.create_superuser("ann-view", "v@example.invalid", "x")
        self.client.force_login(staff)
        response = self.client.get(
            reverse("admin:annotations_annotationset_change", args=[annotation_set.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="_save"')


class JobActionSafetyTests(TestCase):
    """One click may not become five hundred cluster submissions."""

    def setUp(self):
        self.staff = User.objects.create_superuser(
            "job-admin", "j@example.invalid", "x"
        )
        self.client.force_login(self.staff)
        self.project = _project("job-actions")
        self.folder = Folder.objects.create(name="F", project=self.project)
        # A modality only dispatches when a step declares how to process it.
        modality = Modality.objects.create(slug="cbct-actions", name="CBCT actions")
        ProcessingStep.objects.create(
            modality=modality, name="Step", slug="cbct-actions", is_enabled=True
        )
        patcher = mock.patch("common.signals.celery_app.send_task")
        self.send_task = patcher.start()
        self.addCleanup(patcher.stop)
        self.url = reverse("admin:common_job_changelist")

    def _job(self, status):
        patient = Patient.objects.create(project=self.project, folder=self.folder)
        job = Job.objects.create(
            modality_slug="cbct-actions", domain="maxillo", patient=patient
        )
        Job.objects.filter(pk=job.pk).update(status=status)
        return Job.objects.get(pk=job.pk)

    def _post(self, action, jobs, **extra):
        return self.client.post(
            self.url,
            {
                "action": action,
                "_selected_action": [str(job.pk) for job in jobs],
                **extra,
            },
        )

    def test_retry_asks_before_dispatching_anything(self):
        jobs = [self._job("failed") for _ in range(3)]
        self.send_task.reset_mock()
        response = self._post("retry_failed_jobs", jobs)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "dispatches 3 job(s) to the compute cluster")
        self.send_task.assert_not_called()
        self.assertEqual(Job.objects.filter(status="retrying").count(), 0)

    def test_retry_runs_once_confirmed(self):
        jobs = [self._job("failed") for _ in range(2)]
        self.send_task.reset_mock()
        response = self._post("retry_failed_jobs", jobs, confirmed="yes")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Job.objects.filter(status="retrying").count(), 2)
        self.assertEqual(self.send_task.call_count, 2)

    def test_retry_refuses_a_selection_larger_than_the_cap(self):
        from common.admin import JOB_RETRY_CAP

        jobs = [self._job("failed") for _ in range(JOB_RETRY_CAP + 1)]
        self.send_task.reset_mock()
        response = self._post("retry_failed_jobs", jobs, confirmed="yes")
        self.assertEqual(response.status_code, 302)
        self.send_task.assert_not_called()
        self.assertEqual(Job.objects.filter(status="retrying").count(), 0)

    def test_cancelling_is_distinguishable_from_failing(self):
        from common.admin import JOB_CANCELLED_MARKER

        cancelled = self._job("pending")
        failed = self._job("failed")
        self.send_task.reset_mock()
        response = self._post("cancel_pending_jobs", [cancelled], confirmed="yes")
        self.assertEqual(response.status_code, 302)
        cancelled.refresh_from_db()
        failed.refresh_from_db()
        # No new dispatch: the bulk update deliberately bypasses the signal.
        self.send_task.assert_not_called()
        self.assertIn(JOB_CANCELLED_MARKER, cancelled.error_logs)
        self.assertNotIn(JOB_CANCELLED_MARKER, failed.error_logs or "")

    def test_clearing_dependencies_asks_first(self):
        first, second = self._job("pending"), self._job("dependency")
        second.dependencies.add(first)
        response = self._post("clear_dependencies", [second])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be reconstructed")
        self.assertEqual(second.dependencies.count(), 1)

    def test_check_dependencies_asks_first(self):
        job = self._job("dependency")
        response = self._post("check_dependencies", [job])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "dispatched to the cluster at once")
        job.refresh_from_db()
        self.assertEqual(job.status, "dependency")


class DemoSwitchTests(TestCase):
    """``is_demo`` publishes a folder to anonymous readers; it has to ask."""

    def setUp(self):
        self.staff = User.objects.create_superuser(
            "demo-admin", "d@example.invalid", "x"
        )
        self.client.force_login(self.staff)
        self.project = _project("demo-switch")
        self.folder = Folder.objects.create(name="F", project=self.project)

    def test_publishing_requires_confirmation(self):
        url = reverse("admin:maxillo_folder_changelist")
        response = self.client.post(
            url,
            {"action": "publish_to_demo", "_selected_action": [str(self.folder.pk)]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Anyone on the internet")
        self.folder.refresh_from_db()
        self.assertFalse(self.folder.is_demo)

        response = self.client.post(
            url,
            {
                "action": "publish_to_demo",
                "_selected_action": [str(self.folder.pk)],
                "confirmed": "yes",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.folder.refresh_from_db()
        self.assertTrue(self.folder.is_demo)
