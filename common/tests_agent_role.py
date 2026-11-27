"""The ``agent`` role: writes like an annotator, never deletes.

* the role sets and the project-level delete check;
* a guard over the URL table: a new route whose name says it deletes, clears,
  resets, purges, removes or retires must be classified in ``DESTRUCTIVE_ROUTES``
  below, so nobody adds one that an agent can reach by accident.

Endpoint-level tests live with the domains: ``maxillo.tests_agent_role`` and
``cardiology.tests.AgentRoleTests``.
"""
import re

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver

from common.models import Project, ProjectAccess
from common.permissions import (
    DELETE_ROLES,
    READ_ROLES,
    WRITE_ROLES,
    user_can_delete_in_project,
)


class RoleSetTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="agent-p", slug="agent-p", domain="maxillo")

    def _user(self, role, project=None):
        user = User.objects.create_user(username=f"u-{role}", password="pw")
        ProjectAccess.objects.create(user=user, project=project or self.project, role=role)
        return user

    def test_agent_reads_and_writes_but_does_not_delete(self):
        self.assertIn("agent", READ_ROLES)
        self.assertIn("agent", WRITE_ROLES)
        self.assertNotIn("agent", DELETE_ROLES)
        self.assertEqual(DELETE_ROLES, {"annotator", "admin"})

    def test_project_delete_check_role_by_role(self):
        for role, expected in {
            "viewer": False, "agent": False, "annotator": True, "admin": True,
        }.items():
            with self.subTest(role):
                self.assertEqual(user_can_delete_in_project(self._user(role), self.project), expected)

    def test_no_access_and_anonymous_cannot_delete(self):
        from django.contrib.auth.models import AnonymousUser

        stranger = User.objects.create_user(username="stranger", password="pw")
        self.assertFalse(user_can_delete_in_project(stranger, self.project))
        self.assertFalse(user_can_delete_in_project(AnonymousUser(), self.project))

    def test_staff_may_delete_regardless_of_role(self):
        agent = self._user("agent")
        agent.is_staff = True
        agent.save()
        self.assertTrue(user_can_delete_in_project(agent, self.project))

    def test_an_agent_in_one_project_is_not_an_agent_elsewhere(self):
        agent = self._user("agent")
        other = Project.objects.create(name="other", slug="other", domain="maxillo")
        ProjectAccess.objects.create(user=agent, project=other, role="annotator")
        self.assertFalse(user_can_delete_in_project(agent, self.project))
        self.assertTrue(user_can_delete_in_project(agent, other))

    def test_the_check_takes_a_project_only(self):
        with self.assertRaises(TypeError):
            user_can_delete_in_project(self._user("annotator"), "maxillo")

    def test_model_helpers(self):
        access = ProjectAccess.objects.get(user=self._user("agent"), project=self.project)
        self.assertTrue(access.is_annotator())
        self.assertTrue(access.can_upload_scans())
        self.assertFalse(access.can_delete_data())
        self.assertEqual(access.get_role_display(), "Agent")
        annotator = ProjectAccess.objects.get(user=self._user("annotator"), project=self.project)
        self.assertTrue(annotator.can_delete_data())


# Every routed name that reads as destructive, and the rule that keeps an agent out.
# "delete" = needs DELETE_ROLES (refuses an agent); "admin" = project admin / staff
# only; "owner" = the caller's own export, which is a copy, not project data.
DESTRUCTIVE_ROUTES = {
    "delete_patient": "delete",
    "bulk_delete_patients": "admin",
    "bulk_purge_patients": "admin",
    "delete_folder": "admin",
    "remove_patients_from_folder": "admin",
    "remove_patient_tag": "delete",
    "delete_voice_caption": "delete",
    "reset_voice_captions": "delete",
    "classification_clear": "delete",
    "delete_raw_file": "delete",
    "export_delete": "owner",
    "delete_invitation": "admin",
}
DESTRUCTIVE_NAME = re.compile(r"delete|remove|clear|reset|purge|retire|discard|wipe|destroy")
NOT_DESTRUCTIVE = {
    # Django's password-reset flow and the admin site's own views.
    "password_reset", "password_reset_done", "password_reset_confirm", "password_reset_complete",
}


def _route_names(resolver=None):
    for entry in (resolver or get_resolver()).url_patterns:
        if isinstance(entry, URLResolver):
            if entry.namespace == "admin":
                continue
            yield from _route_names(entry)
        elif isinstance(entry, URLPattern) and entry.name:
            yield entry.name


class DestructiveRouteGuardTests(TestCase):
    def test_every_destructive_route_is_classified(self):
        unclassified = sorted(
            {
                name for name in _route_names()
                if DESTRUCTIVE_NAME.search(name)
                and name not in DESTRUCTIVE_ROUTES
                and name not in NOT_DESTRUCTIVE
            }
        )
        self.assertEqual(
            unclassified, [],
            "New destructive route(s): decide whether an agent may use them, gate them "
            "with get_patient_for(..., 'delete') / user_can_delete_in_project, and list "
            "them in DESTRUCTIVE_ROUTES.",
        )
