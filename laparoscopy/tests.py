from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from common.models import FileRegistry, Project, ProjectAccess
from common.permissions import filter_folders_for_user, filter_patients_for_user
from laparoscopy.models import Folder, Patient


class LaparoscopyProjectAccessTests(TestCase):
    """Project-level ACL for laparoscopy (mirrors maxillo/brain behavior)."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.get_or_create(
            slug="laparoscopy", defaults={"name": "Laparoscopy", "domain": "laparoscopy"}
        )[0]
        self.admin = User.objects.create_user(username="lap-acl-admin", password="pw")
        ProjectAccess.objects.create(user=self.admin, project=self.project, role="admin")
        self.member = User.objects.create_user(username="lap-acl-member", password="pw")
        ProjectAccess.objects.create(user=self.member, project=self.project, role="viewer")
        self.outsider = User.objects.create_user(username="lap-acl-outsider", password="pw")

        self.folder_a = Folder.objects.create(name="Folder A", project=self.project)
        self.folder_b = Folder.objects.create(name="Folder B", project=self.project)
        self.patient_a = Patient.objects.create(name="PA", folder=self.folder_a, project=self.project)
        self.patient_b = Patient.objects.create(name="PB", folder=self.folder_b, project=self.project)

    def test_member_without_project_access_sees_no_folders(self):
        qs = filter_folders_for_user(self.outsider, Folder.objects.all(), "laparoscopy")
        self.assertEqual(qs.count(), 0)

    def test_member_without_project_access_sees_no_patients(self):
        qs = filter_patients_for_user(self.outsider, Patient.objects.all(), "laparoscopy")
        self.assertEqual(qs.count(), 0)

    def test_project_member_sees_folders(self):
        qs = filter_folders_for_user(self.member, Folder.objects.all(), "laparoscopy")
        self.assertIn(self.folder_a, qs)
        self.assertIn(self.folder_b, qs)

    def test_project_member_sees_patients(self):
        qs = filter_patients_for_user(self.member, Patient.objects.all(), "laparoscopy")
        self.assertEqual(qs.count(), 2)

    def test_project_admin_sees_all_folders(self):
        qs = filter_folders_for_user(self.admin, Folder.objects.all(), "laparoscopy")
        self.assertIn(self.folder_a, qs)
        self.assertIn(self.folder_b, qs)

    def test_project_admin_sees_all_patients(self):
        qs = filter_patients_for_user(self.admin, Patient.objects.all(), "laparoscopy")
        self.assertEqual(qs.count(), 2)


class LaparoscopyPatientPageTests(TestCase):
    """The page is a plain player plus the shared file panes -- nothing to annotate with."""

    def setUp(self):
        self.project = Project.objects.get_or_create(
            slug="laparoscopy", defaults={"name": "Laparoscopy", "domain": "laparoscopy"}
        )[0]
        self.user = User.objects.create_user(username="lap-page-admin", password="pw")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        folder = Folder.objects.create(name="F", project=self.project)
        self.patient = Patient.objects.create(name="P", folder=folder, project=self.project)
        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def _get(self):
        return self.client.get(
            reverse("laparoscopy:patient_detail", kwargs={"patient_id": self.patient.patient_id})
        )

    def test_a_patient_without_video_says_so(self):
        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No video uploaded for this patient.")
        self.assertNotContains(response, "<video")

    def test_a_patient_with_video_gets_a_plain_player_and_no_annotator(self):
        FileRegistry.objects.create(
            domain="laparoscopy",
            file_type="video_raw",
            laparoscopy_patient=self.patient,
            file_path="laparoscopy/case.mp4",
            file_size=1,
            file_hash="0" * 64,
        )

        response = self._get()

        self.assertContains(response, "<video")
        self.assertNotContains(response, "annotation-toggle-btn", msg_prefix="no probe, no annotator")
        self.assertNotContains(response, "imageSegmentData")

    def _video(self, **metadata):
        return FileRegistry.objects.create(
            domain="laparoscopy",
            file_type="video_raw",
            laparoscopy_patient=self.patient,
            file_path="laparoscopy/case.mp4",
            file_size=1,
            file_hash="0" * 64,
            metadata=metadata,
        )

    def _subsampled_video(self, **metadata):
        return FileRegistry.objects.create(
            domain="laparoscopy",
            file_type="video_processed",
            subtype="subsampled",
            laparoscopy_patient=self.patient,
            file_path="laparoscopy/subsampled.mp4",
            file_size=1,
            file_hash="1" * 64,
            metadata=metadata,
        )

    PROBE = {"probe": {"width": 1920, "height": 1080, "fps": 30.0, "frame_count": 5608}}

    def _enable(self):
        from common.models import AnnotationMethod

        self.project.annotation_methods.add(
            AnnotationMethod.objects.get(slug="image_segmentation")
        )

    def test_annotation_mode_needs_both_the_method_and_a_recorded_probe(self):
        self._video(**self.PROBE)
        # The shared 'laparoscopy' project may already carry the method (a data migration
        # enables it on existing projects), so start from a project without it.
        self.project.annotation_methods.clear()

        # Probe but no method: the server would refuse every save, so offer nothing.
        self.assertNotContains(self._get(), "annotation-toggle-btn")

        self._enable()
        # The compressed player is not a valid annotation source: only the subsampled
        # derivative has the frame clock the masks are keyed to.
        self.assertNotContains(self._get(), "annotation-toggle-btn")

        video = self._subsampled_video(**self.PROBE)
        response = self._get()
        self.assertContains(response, "annotation-toggle-btn")
        self.assertContains(response, 'id="video-timeline-range"')
        self.assertContains(response, 'id="video-start"')
        self.assertContains(response, 'id="video-stop"')
        self.assertContains(response, 'data-video-seek="-10"')
        self.assertContains(response, 'data-video-seek="10"')
        payload = response.context["image_segment_data"]
        self.assertEqual(
            payload,
            {
                "patientId": self.patient.patient_id,
                "projectNamespace": "laparoscopy",
                "fileId": video.pk,
                "width": 1920,
                "height": 1080,
                "fps": 30.0,
                "frameCount": 5608,
                "videoUrl": reverse(
                    "laparoscopy:api_serve_file", kwargs={"file_id": video.pk}
                ),
                "canModify": True,
            },
        )

        # Method but no probe: a guessed frame rate puts masks on the wrong frames.
        video.metadata = {}
        video.save(update_fields=["metadata"])
        self.assertNotContains(self._get(), "annotation-toggle-btn")

    def test_stored_mask_files_are_not_listed_as_downloadable_files(self):
        video = self._video(**self.PROBE)
        for index in range(3):
            FileRegistry.objects.create(
                domain="laparoscopy",
                file_type="annotation_mask",
                laparoscopy_patient=self.patient,
                file_path=f"annotations/image_segmentation/patient_x/f{video.pk}t{index}_abc.npz",
                file_size=1,
                file_hash=str(index) * 64,
            )

        response = self._get()

        listed = [row["id"] for group in response.context["patient_files"].values() for row in group]
        self.assertIn(video.pk, listed)
        self.assertEqual(len(listed), 1, "masks are storage behind the record, not files")
        self.assertNotContains(response, "annotations/image_segmentation")

    def test_a_viewer_gets_the_surface_but_cannot_modify(self):
        self._video(**self.PROBE)
        self._subsampled_video(**self.PROBE)
        self._enable()
        ProjectAccess.objects.filter(user=self.user).update(role="viewer")
        self.assertFalse(self._get().context["image_segment_data"]["canModify"])
