"""The ``agent`` role on patients: writes like an annotator, never deletes.

* the patient-level permission helpers, role by role;
* every endpoint an annotator can use to delete, clear or replace data refuses an
  agent and leaves the data where it was (the annotator still succeeds, so the
  refusal is the role and not a broken fixture).

The role sets and the destructive-route guard are in ``common.tests_agent_role``;
the cardiology-only routes in ``cardiology.tests.AgentRoleTests``.
"""
import io
import json
import uuid
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from common.models import FileRegistry, Project, ProjectAccess
from common.permissions import (
    get_patient_for,
    user_can_delete_in_project,
    user_can_delete_single_patient,
    user_can_read_patient,
    user_can_view_caption_content,
    user_can_write_patient_annotations,
)
from maxillo.models import Folder, Patient, Tag, VoiceCaption


def _png():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    return SimpleUploadedFile("edit.png", buffer.getvalue(), content_type="image/png")


class AgentRoleTestCase(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="agent-p", slug="agent-p", domain="maxillo")
        self.folder = Folder.objects.create(name="F", project=self.project)
        self.patient = Patient.objects.create(project=self.project, folder=self.folder, name="P1")
        self.viewer = self._user("viewer")
        self.agent = self._user("agent")
        self.annotator = self._user("annotator")
        self.admin = self._user("admin")

    def _user(self, role):
        user = User.objects.create_user(username=f"u-{role}", password="pw")
        ProjectAccess.objects.create(user=user, project=self.project, role=role)
        return user

    def _login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def _file(self, file_type, **extra):
        return FileRegistry.objects.create(
            patient=self.patient, file_type=file_type, domain="maxillo",
            file_path=f"maxillo/{file_type}/{uuid.uuid4()}.bin", file_size=1, file_hash="0" * 64,
            **extra,
        )


class PatientPermissionTests(AgentRoleTestCase):
    def test_helpers_role_by_role(self):
        expected = {
            # role: (read, write, delete)
            "viewer": (True, False, False),
            "agent": (True, True, False),
            "annotator": (True, True, True),
            "admin": (True, True, True),
        }
        for role, (read, write, delete) in expected.items():
            user = getattr(self, role)
            with self.subTest(role):
                self.assertEqual(user_can_read_patient(user, self.patient), read)
                self.assertEqual(user_can_write_patient_annotations(user, self.patient), write)
                self.assertEqual(user_can_delete_in_project(user, self.project), delete)
                self.assertEqual(
                    user_can_delete_single_patient(user, self.folder, self.project), delete
                )

    def test_get_patient_for_delete_is_403_for_an_agent(self):
        from django.core.exceptions import PermissionDenied

        self.assertEqual(get_patient_for(self.agent, Patient, self.patient.pk, "write"), self.patient)
        with self.assertRaises(PermissionDenied):
            get_patient_for(self.agent, Patient, self.patient.pk, "delete")
        self.assertEqual(get_patient_for(self.annotator, Patient, self.patient.pk, "delete"), self.patient)

    def test_an_agent_is_blinded_to_others_captions_like_an_annotator(self):
        caption = VoiceCaption.objects.create(patient=self.patient, user=self.admin, duration=1.0)
        self.assertFalse(user_can_view_caption_content(self.agent, caption))
        self.assertFalse(user_can_view_caption_content(self.annotator, caption))
        self.assertTrue(user_can_view_caption_content(self.viewer, caption))


class AgentCanWriteTests(AgentRoleTestCase):
    def test_agent_can_tag_a_patient(self):
        self._login(self.agent)
        response = self.client.post(
            reverse("maxillo:add_patient_tag", args=[self.patient.patient_id]),
            json.dumps({"tag": "from-agent"}), content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("from-agent", self.patient.tag_names())

    def test_agent_can_save_a_first_rgb_edit(self):
        raw = self._file("intraoral_raw")
        self._login(self.agent)
        with mock.patch("maxillo.views.patient_data.get_object_storage"):
            response = self.client.post(
                reverse("maxillo:save_rgb_image_edit", args=[self.patient.patient_id]),
                {"modality_slug": "intraoral-photo", "source_file_id": raw.pk, "image": _png()},
            )
        self.assertEqual(response.status_code, 200, response.content)


class AgentCannotDeleteTests(AgentRoleTestCase):
    """Each destructive endpoint an annotator can reach: agent refused, annotator not."""

    def test_delete_patient(self):
        url = reverse("maxillo:delete_patient", args=[self.patient.patient_id])
        self._login(self.agent)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.patient.refresh_from_db()
        self.assertFalse(self.patient.deleted)

        self._login(self.annotator)
        self.assertEqual(self.client.post(url).status_code, 200)
        self.patient.refresh_from_db()
        self.assertTrue(self.patient.deleted)

    def test_patient_list_hides_the_delete_button_from_an_agent(self):
        self._login(self.agent)
        response = self.client.get(reverse("maxillo:patient_list"))
        self.assertEqual(response.status_code, 200)
        rows = [row for row in response.context["page_obj"].object_list]
        self.assertTrue(rows)
        self.assertFalse(any(row["can_delete"] for row in rows))

    def test_remove_patient_tag(self):
        self.patient.tags.add(Tag.objects.create(name="keep-me"))
        url = reverse("maxillo:remove_patient_tag", args=[self.patient.patient_id])
        body = json.dumps({"tag": "keep-me"})

        self._login(self.agent)
        self.assertEqual(self.client.post(url, body, content_type="application/json").status_code, 403)
        self.assertIn("keep-me", self.patient.tag_names())

        self._login(self.annotator)
        self.assertEqual(self.client.post(url, body, content_type="application/json").status_code, 200)
        self.assertNotIn("keep-me", self.patient.tag_names())

    def test_delete_own_voice_caption(self):
        caption = VoiceCaption.objects.create(patient=self.patient, user=self.agent, duration=1.0)
        self._login(self.agent)
        response = self.client.delete(
            reverse("maxillo:delete_voice_caption", args=[self.patient.patient_id, caption.pk])
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(VoiceCaption.objects.filter(pk=caption.pk).exists())

    def test_delete_raw_file(self):
        raw = self._file("cbct_raw")
        self._login(self.agent)
        response = self.client.post(
            reverse("maxillo:delete_raw_file", kwargs={"patient_id": self.patient.pk, "file_id": raw.pk})
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(FileRegistry.objects.filter(pk=raw.pk).exists())

    def test_replacing_scans_from_the_patient_page(self):
        self._login(self.agent)
        with mock.patch("maxillo.file_utils.save_cbct_to_dataset") as save_cbct:
            response = self.client.post(
                reverse("maxillo:patient_detail", args=[self.patient.patient_id]),
                {"action": "update_files", "cbct": SimpleUploadedFile("c.nii.gz", b"x")},
            )
        self.assertEqual(response.status_code, 302)
        save_cbct.assert_not_called()

    def test_replacing_an_existing_rgb_edit(self):
        raw = self._file("intraoral_raw")
        previous = self._file("intraoral-photo_processed", metadata={"source_file_id": raw.pk})
        self._login(self.agent)
        with mock.patch("maxillo.views.patient_data.get_object_storage") as storage:
            response = self.client.post(
                reverse("maxillo:save_rgb_image_edit", args=[self.patient.patient_id]),
                {"modality_slug": "intraoral-photo", "source_file_id": raw.pk, "image": _png()},
            )
        self.assertEqual(response.status_code, 403)
        storage.return_value.upload_file.assert_not_called()
        storage.return_value.delete.assert_not_called()
        self.assertTrue(FileRegistry.objects.filter(pk=previous.pk).exists())
