"""Raw data freezes once annotations exist -- helper, admin and app endpoints.

Covers ``common.annotation_lock`` and its three enforcement sites. The admin half
is the first admin-focused test in the project; it follows the project/modality
setup shape of ``maxillo.tests_bulk_upload`` and the hand-built ``FileRegistry``
rows of ``maxillo.tests_rerun_steps``.
"""
import uuid

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from brain.models import Folder as BrainFolder, Patient as BrainPatient
from brain.models import VoiceCaption as BrainVoiceCaption
from common.annotation_lock import (
    annotation_lock_reasons,
    is_raw_file_type,
    panoramic_is_locked,
    raw_data_is_locked,
)
from common.models import FileRegistry, Modality, Project, ProjectAccess
from laparoscopy.models import (
    Folder as LaparoFolder,
    Patient as LaparoPatient,
    QuadrantClassificationMarker,
    QuadrantType,
    RegionAnnotation,
    RegionType,
)
from maxillo.models import (
    Classification,
    Folder,
    IntraoralToothSegmentation,
    PanoramicState,
    Patient,
    VoiceCaption,
)


def _project(slug, domain):
    return Project.objects.create(name=slug, slug=slug, domain=domain)


def _registry(patient, file_type, domain="maxillo", **extra):
    """A FileRegistry row for `patient`, keyed uniquely (file_path is unique)."""
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


def _panoramic_state(patient, geometry_source):
    """A minimally-valid PanoramicState; only geometry_source matters here."""
    return PanoramicState.objects.create(
        patient=patient,
        source_file=_registry(patient, "cbct_processed"),
        source_file_key="primary",
        source_file_hash="1" * 64,
        mip_file=_registry(patient, "panoramic_processed"),
        raysum_file=_registry(patient, "panoramic_processed"),
        axial_slice=10,
        volume_shape=[100, 100, 100],
        spline={"points": []},
        geometry_source=geometry_source,
        default_mode="mip",
        request_hash="2" * 64,
    )


class LockTriggerTests(TestCase):
    """What does and does not count as "annotations were produced"."""

    def setUp(self):
        self.maxillo_project = _project("lock-maxillo", "maxillo")
        self.folder = Folder.objects.create(name="F", project=self.maxillo_project)
        self.patient = Patient.objects.create(
            project=self.maxillo_project, folder=self.folder
        )
        self.user = User.objects.create_user(username="lock-tests", password="x")

    def test_a_fresh_patient_is_open_in_every_domain(self):
        laparo_project = _project("lock-laparo", "laparoscopy")
        brain_project = _project("lock-brain", "brain")
        laparo = LaparoPatient.objects.create(
            project=laparo_project,
            folder=LaparoFolder.objects.create(name="F", project=laparo_project),
        )
        brain = BrainPatient.objects.create(
            project=brain_project,
            folder=BrainFolder.objects.create(name="F", project=brain_project),
        )

        for patient in (self.patient, laparo, brain):
            self.assertEqual(annotation_lock_reasons(patient), [])
            self.assertFalse(raw_data_is_locked(patient))

    def test_none_is_open(self):
        self.assertEqual(annotation_lock_reasons(None), [])

    def test_a_voice_caption_locks(self):
        VoiceCaption.objects.create(patient=self.patient, user=self.user, duration=1.0)
        self.assertTrue(raw_data_is_locked(self.patient))
        self.assertIn("voice captions", annotation_lock_reasons(self.patient))

    def test_an_occlusion_classification_locks(self):
        Classification.objects.create(patient=self.patient)
        self.assertTrue(raw_data_is_locked(self.patient))

    def test_tooth_segmentation_locks(self):
        IntraoralToothSegmentation.objects.create(
            patient=self.patient,
            image_file=_registry(self.patient, "intraoral_raw"),
            teeth={"11": [[[0, 0], [1, 1]]]},
        )
        self.assertTrue(raw_data_is_locked(self.patient))

    def test_ios_landmarks_lock(self):
        _registry(self.patient, "ios_landmarks")
        self.assertTrue(raw_data_is_locked(self.patient))

    def test_predicted_landmarks_do_not_lock(self):
        # Machine output is not annotation work.
        _registry(self.patient, "ios_landmarks_prediction")
        self.assertEqual(annotation_lock_reasons(self.patient), [])

    def test_raw_and_processed_files_alone_do_not_lock(self):
        _registry(self.patient, "cbct_raw")
        _registry(self.patient, "cbct_processed")
        self.assertFalse(raw_data_is_locked(self.patient))

    def test_an_edited_panoramic_locks_raw_data_but_not_the_panoramic(self):
        _panoramic_state(self.patient, "custom_cp")
        self.assertTrue(raw_data_is_locked(self.patient))
        # Its own edit must not freeze the editor, or one edit would be the last.
        self.assertFalse(panoramic_is_locked(self.patient))

    def test_an_auto_panoramic_does_not_lock(self):
        _panoramic_state(self.patient, "auto")
        self.assertFalse(raw_data_is_locked(self.patient))

    def test_another_annotation_locks_the_panoramic(self):
        _panoramic_state(self.patient, "custom_cp")
        VoiceCaption.objects.create(patient=self.patient, user=self.user, duration=1.0)
        self.assertTrue(panoramic_is_locked(self.patient))

    def test_laparoscopy_markers_and_regions_lock(self):
        project = _project("lock-laparo-ann", "laparoscopy")
        folder = LaparoFolder.objects.create(name="F", project=project)
        marked = LaparoPatient.objects.create(project=project, folder=folder)
        QuadrantClassificationMarker.objects.create(
            patient=marked,
            quadrant_type=QuadrantType.objects.create(project=project, name="Q1"),
            time_ms=10,
        )
        self.assertTrue(raw_data_is_locked(marked))

        regioned = LaparoPatient.objects.create(project=project, folder=folder)
        RegionAnnotation.objects.create(
            patient=regioned,
            region_type=RegionType.objects.create(project=project, name="R1"),
            tool="brush",
            points=[[0, 0]],
        )
        self.assertTrue(raw_data_is_locked(regioned))

    def test_a_brain_voice_caption_locks(self):
        project = _project("lock-brain-ann", "brain")
        patient = BrainPatient.objects.create(
            project=project,
            folder=BrainFolder.objects.create(name="F", project=project),
        )
        BrainVoiceCaption.objects.create(patient=patient, user=self.user, duration=1.0)
        self.assertTrue(raw_data_is_locked(patient))

    def test_is_raw_file_type(self):
        self.assertTrue(is_raw_file_type("cbct_raw"))
        self.assertTrue(is_raw_file_type("ios_raw_upper".replace("_upper", "")))
        self.assertFalse(is_raw_file_type("cbct_processed"))
        self.assertFalse(is_raw_file_type("ios_landmarks"))
        self.assertFalse(is_raw_file_type(None))


class FileRegistryAdminLockTests(TestCase):
    """The admin freeze, and the superuser override that survives it."""

    def setUp(self):
        self.project = _project("lock-admin", "maxillo")
        self.folder = Folder.objects.create(name="F", project=self.project)
        self.patient = Patient.objects.create(project=self.project, folder=self.folder)
        self.open_patient = Patient.objects.create(
            project=self.project, folder=self.folder
        )
        self.annotator = User.objects.create_user(username="admin-ann", password="x")
        VoiceCaption.objects.create(
            patient=self.patient, user=self.annotator, duration=1.0
        )

        self.raw = _registry(self.patient, "cbct_raw")
        self.processed = _registry(self.patient, "cbct_processed")
        self.open_raw = _registry(self.open_patient, "cbct_raw")

        self.staff = User.objects.create_user(
            username="lock-staff", password="x", is_staff=True
        )
        self.staff.user_permissions.set(Permission.objects.all())
        self.superuser = User.objects.create_superuser(
            username="lock-super", password="x", email="s@example.com"
        )

    def _change_url(self, obj):
        return reverse("admin:common_fileregistry_change", args=[obj.pk])

    def _delete_url(self, obj):
        return reverse("admin:common_fileregistry_delete", args=[obj.pk])

    def test_staff_cannot_open_a_locked_raw_row_for_editing(self):
        self.client.force_login(self.staff)
        response = self.client.get(self._change_url(self.raw))
        self.assertEqual(response.status_code, 200)
        # Django renders the read-only view form: no save row at all.
        self.assertNotContains(response, 'name="_save"')

    def test_staff_cannot_delete_a_locked_raw_row(self):
        self.client.force_login(self.staff)
        response = self.client.post(self._delete_url(self.raw), {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(FileRegistry.objects.filter(pk=self.raw.pk).exists())

    def test_staff_cannot_register_a_new_raw_file_for_a_locked_patient(self):
        self.client.force_login(self.staff)
        before = FileRegistry.objects.count()
        response = self.client.post(
            reverse("admin:common_fileregistry_add"),
            {
                "file_type": "cbct_raw",
                "file_path": "maxillo/raw/cbct/sneaky.nii.gz",
                "file_size": "10",
                "file_hash": "3" * 64,
                "subtype": "",
                "domain": "maxillo",
                "patient": str(self.patient.pk),
                "metadata": "{}",
            },
        )
        self.assertEqual(response.status_code, 200)  # redisplayed with the error
        self.assertContains(response, "can no longer be changed")
        self.assertEqual(FileRegistry.objects.count(), before)

    def test_a_processed_row_on_a_locked_patient_stays_editable(self):
        self.client.force_login(self.staff)
        response = self.client.get(self._change_url(self.processed))
        self.assertContains(response, 'name="_save"')

    def test_a_raw_row_on_an_unannotated_patient_stays_editable(self):
        self.client.force_login(self.staff)
        response = self.client.get(self._change_url(self.open_raw))
        self.assertContains(response, 'name="_save"')

    def test_a_superuser_may_still_delete_a_locked_raw_row(self):
        self.client.force_login(self.superuser)
        response = self.client.post(self._delete_url(self.raw), {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(FileRegistry.objects.filter(pk=self.raw.pk).exists())

    def test_a_superuser_may_still_open_a_locked_raw_row(self):
        self.client.force_login(self.superuser)
        response = self.client.get(self._change_url(self.raw))
        self.assertContains(response, 'name="_save"')


class LaparoscopyPatientAdminLockTests(TestCase):
    """Laparoscopy keeps raw scans in FileFields on the patient, so lock those."""

    def setUp(self):
        from laparoscopy.admin import PatientAdmin

        self.admin = PatientAdmin(LaparoPatient, __import__("django.contrib.admin", fromlist=["site"]).site)
        self.project = _project("lock-laparo-admin", "laparoscopy")
        self.folder = LaparoFolder.objects.create(name="F", project=self.project)
        self.patient = LaparoPatient.objects.create(
            project=self.project, folder=self.folder
        )
        self.open_patient = LaparoPatient.objects.create(
            project=self.project, folder=self.folder
        )
        QuadrantClassificationMarker.objects.create(
            patient=self.patient,
            quadrant_type=QuadrantType.objects.create(project=self.project, name="Q1"),
            time_ms=10,
        )
        self.staff = User.objects.create_user(
            username="laparo-staff", password="x", is_staff=True
        )
        self.superuser = User.objects.create_superuser(
            username="laparo-super", password="x", email="s2@example.com"
        )

    def _readonly_for(self, user, patient):
        request = type("R", (), {"user": user})()
        return self.admin.get_readonly_fields(request, patient)

    def test_raw_scan_fields_are_frozen_for_staff_on_a_locked_patient(self):
        readonly = self._readonly_for(self.staff, self.patient)
        for field in ("upper_scan_raw", "lower_scan_raw", "cbct"):
            self.assertIn(field, readonly)
        # Normalized scans are pipeline output, not raw input.
        self.assertNotIn("upper_scan_norm", readonly)

    def test_raw_scan_fields_stay_editable_on_an_unannotated_patient(self):
        self.assertNotIn("cbct", self._readonly_for(self.staff, self.open_patient))

    def test_a_superuser_keeps_the_override(self):
        self.assertNotIn("cbct", self._readonly_for(self.superuser, self.patient))


class RawFileEndpointLockTests(TestCase):
    """add_raw_file / delete_raw_file refuse an annotated patient, for everyone."""

    def setUp(self):
        self.project = _project("lock-endpoint", "maxillo")
        self.project.modalities.set([
            Modality.objects.get_or_create(slug="cbct", defaults={"name": "CBCT"})[0]
        ])
        self.folder = Folder.objects.create(name="F", project=self.project)
        self.patient = Patient.objects.create(project=self.project, folder=self.folder)
        self.raw = _registry(self.patient, "cbct_raw")

        # A project admin: the widest role the app has, and still refused.
        self.user = User.objects.create_user(username="endpoint-admin", password="x")
        ProjectAccess.objects.create(
            user=self.user, project=self.project, role="admin"
        )
        VoiceCaption.objects.create(patient=self.patient, user=self.user, duration=1.0)

        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def test_delete_raw_file_is_refused(self):
        response = self.client.post(
            reverse(
                "maxillo:delete_raw_file",
                kwargs={"patient_id": self.patient.pk, "file_id": self.raw.pk},
            )
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["raw_locked"])
        self.assertTrue(FileRegistry.objects.filter(pk=self.raw.pk).exists())

    def test_add_raw_file_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        before = FileRegistry.objects.count()
        response = self.client.post(
            reverse("maxillo:add_raw_file", kwargs={"patient_id": self.patient.pk}),
            {
                "file_type": "cbct_raw",
                "file": SimpleUploadedFile("scan.nii.gz", b"bytes"),
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["raw_locked"])
        self.assertEqual(FileRegistry.objects.count(), before)

    def test_an_unannotated_patient_is_not_refused(self):
        open_patient = Patient.objects.create(
            project=self.project, folder=self.folder
        )
        open_raw = _registry(open_patient, "cbct_raw")
        response = self.client.post(
            reverse(
                "maxillo:delete_raw_file",
                kwargs={"patient_id": open_patient.pk, "file_id": open_raw.pk},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(FileRegistry.objects.filter(pk=open_raw.pk).exists())


class PanoramicSaveLockTests(TestCase):
    """An existing arch is never replaced once other annotations exist."""

    def setUp(self):
        self.project = _project("lock-panoramic", "maxillo")
        self.folder = Folder.objects.create(name="F", project=self.project)
        self.patient = Patient.objects.create(project=self.project, folder=self.folder)
        self.user = User.objects.create_user(username="pano-admin", password="x")
        ProjectAccess.objects.create(
            user=self.user, project=self.project, role="admin"
        )
        VoiceCaption.objects.create(patient=self.patient, user=self.user, duration=1.0)
        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def _post(self):
        return self.client.post(
            reverse(
                "maxillo:save_browser_panoramic",
                kwargs={"patient_id": self.patient.pk},
            ),
            {"state": "{}"},
        )

    def test_replacing_an_existing_arch_is_refused(self):
        _panoramic_state(self.patient, "auto")
        response = self._post()
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["panoramic_locked"])

    def test_a_first_generation_is_not_refused_by_the_lock(self):
        # No PanoramicState yet, so the automatic default is still allowed: the
        # request gets past the lock and is rejected further down on its
        # (deliberately empty) payload instead. Both refusals are 409s, so the
        # payload is what distinguishes them.
        response = self._post()
        self.assertNotIn("panoramic_locked", response.json())
        self.assertEqual(response.json()["error"], "No active CBCT source")
