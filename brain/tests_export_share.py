"""View-level tests for export share-link expiry (brain domain).

Brain has its own share views, so expiry enforcement is verified separately
from maxillo. Brain's share update endpoint is owner-or-project-admin (not
staff-only), which makes the "never expires is admin-only" rule observable
here: a non-admin owner must get a 400 for expires_in_days="never".
"""

import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from brain.models import Export
from common.models import Project, ProjectAccess


class BrainExportShareTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project, _ = Project.objects.get_or_create(
            slug="brain", defaults={"name": "Brain"}
        )
        cls.owner = User.objects.create_user("brain-owner", password="pw")
        ProjectAccess.objects.create(
            user=cls.owner, project=cls.project, role="standard"
        )
        cls.staff = User.objects.create_user(
            "brain-staff", password="pw", is_staff=True
        )
        ProjectAccess.objects.create(
            user=cls.staff, project=cls.project, role="admin"
        )

    def make_export(self, **kwargs):
        defaults = {
            "user": self.owner,
            "status": "completed",
            "share_mode": "public",
            "share_token": "brain-share-token",
            "shared_at": timezone.now(),
            "file_path": "brain/exports/test.zip",
            "file_size": 123,
            "completed_at": timezone.now(),
        }
        defaults.update(kwargs)
        return Export.objects.create(**defaults)

    def update(self, export, payload):
        url = reverse("brain:export_share_update", args=[export.id])
        return self.client.post(
            url, json.dumps(payload), content_type="application/json"
        )


class BrainSharedExpiryTests(BrainExportShareTestBase):
    def test_expired_link_landing_returns_410(self):
        export = self.make_export(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        response = self.client.get(
            reverse("brain:export_shared_landing", args=[export.share_token])
        )
        self.assertContains(response, "Share Link Expired", status_code=410)

    def test_expired_link_download_returns_410(self):
        export = self.make_export(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        response = self.client.get(
            reverse("brain:export_shared_download", args=[export.share_token])
        )
        self.assertEqual(response.status_code, 410)

    @patch("brain.views.artifact_exists", return_value=True)
    def test_unexpired_link_still_available(self, _exists):
        export = self.make_export(
            expires_at=timezone.now() + timedelta(days=7)
        )
        response = self.client.get(
            reverse("brain:export_shared_landing", args=[export.share_token])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Download ZIP")


class BrainShareUpdateExpiryTests(BrainExportShareTestBase):
    def test_owner_gets_default_expiry(self):
        self.client.login(username="brain-owner", password="pw")
        export = self.make_export(share_token=None, share_mode="private")
        response = self.update(export, {"share_mode": "public"})
        self.assertEqual(response.status_code, 200)
        export.refresh_from_db()
        self.assertIsNotNone(export.expires_at)
        self.assertIsNotNone(export.share_token)

    def test_owner_cannot_set_never(self):
        self.client.login(username="brain-owner", password="pw")
        export = self.make_export()
        response = self.update(
            export, {"share_mode": "public", "expires_in_days": "never"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "staff or project admins", response.json()["error"]
        )

    def test_staff_can_set_never(self):
        self.client.login(username="brain-staff", password="pw")
        export = self.make_export(
            expires_at=timezone.now() + timedelta(days=3)
        )
        response = self.update(
            export, {"share_mode": "public", "expires_in_days": "never"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["expires_at"])
        export.refresh_from_db()
        self.assertIsNone(export.expires_at)

    def test_private_clears_expiry(self):
        self.client.login(username="brain-owner", password="pw")
        export = self.make_export(
            expires_at=timezone.now() + timedelta(days=3)
        )
        response = self.update(export, {"share_mode": "private"})
        self.assertEqual(response.status_code, 200)
        export.refresh_from_db()
        self.assertIsNone(export.expires_at)
        self.assertIsNone(export.share_token)
