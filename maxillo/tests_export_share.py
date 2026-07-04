"""View-level tests for export share-link expiry (maxillo domain).

Covers shared landing/download responses for expired links (410) and the
expiry parameter handling of export_share_update. The pure expiry rules are
tested in common/tests_export_share.py.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from common.models import Project, ProjectAccess
from maxillo.models import Export


class ExportShareTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project, _ = Project.objects.get_or_create(
            slug="maxillo", defaults={"name": "Maxillo"}
        )
        cls.admin = User.objects.create_user(
            "share-admin", password="pw", is_staff=True
        )
        ProjectAccess.objects.create(
            user=cls.admin, project=cls.project, role="admin"
        )

    def make_export(self, **kwargs):
        defaults = {
            "user": self.admin,
            "status": "completed",
            "share_mode": "public",
            "share_token": "test-share-token",
            "shared_at": timezone.now(),
            "file_path": "exports/test.zip",
            "file_size": 123,
            "completed_at": timezone.now(),
        }
        defaults.update(kwargs)
        return Export.objects.create(**defaults)


class SharedLandingExpiryTests(ExportShareTestBase):
    def landing_url(self, token):
        return reverse("maxillo:export_shared_landing", args=[token])

    def download_url(self, token):
        return reverse("maxillo:export_shared_download", args=[token])

    def test_expired_link_landing_returns_410(self):
        export = self.make_export(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        response = self.client.get(self.landing_url(export.share_token))
        self.assertEqual(response.status_code, 410)
        self.assertContains(response, "Share Link Expired", status_code=410)

    def test_expired_link_download_returns_410(self):
        export = self.make_export(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        response = self.client.get(self.download_url(export.share_token))
        self.assertEqual(response.status_code, 410)

    @patch("maxillo.views.export.artifact_exists", return_value=True)
    def test_future_expiry_still_available(self, _exists):
        export = self.make_export(
            expires_at=timezone.now() + timedelta(days=7)
        )
        response = self.client.get(self.landing_url(export.share_token))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Download ZIP")

    @patch("maxillo.views.export.artifact_exists", return_value=True)
    def test_null_expiry_never_expires(self, _exists):
        export = self.make_export(expires_at=None)
        response = self.client.get(self.landing_url(export.share_token))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Download ZIP")

    def test_private_link_still_unavailable_not_expired_wording(self):
        export = self.make_export(share_mode="private")
        response = self.client.get(self.landing_url(export.share_token))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Download Not Available")


class LaparoscopySharedExpiryTests(TestCase):
    """Laparoscopy mounts maxillo's export views (laparoscopy/urls.py) with
    its own Export model resolved via get_domain_models — verify expiry is
    enforced on that path too."""

    def test_expired_link_returns_410_on_laparoscopy_mount(self):
        from laparoscopy.models import Export as LapExport

        user = User.objects.create_user("lap-owner", password="pw")
        export = LapExport.objects.create(
            user=user,
            status="completed",
            share_mode="public",
            share_token="lap-share-token",
            shared_at=timezone.now(),
            file_path="lap/exports/test.zip",
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        landing = self.client.get(
            reverse("laparoscopy:export_shared_landing", args=[export.share_token])
        )
        self.assertContains(landing, "Share Link Expired", status_code=410)
        download = self.client.get(
            reverse("laparoscopy:export_shared_download", args=[export.share_token])
        )
        self.assertEqual(download.status_code, 410)


class ExportShareUpdateExpiryTests(ExportShareTestBase):
    def setUp(self):
        self.client.login(username="share-admin", password="pw")

    def update(self, export, payload):
        url = reverse("maxillo:export_share_update", args=[export.id])
        return self.client.post(
            url, json.dumps(payload), content_type="application/json"
        )

    def assert_days_from_now(self, iso_value, days):
        expected = timezone.now() + timedelta(days=days)
        parsed = datetime.fromisoformat(iso_value)
        self.assertAlmostEqual(
            parsed.timestamp(), expected.timestamp(), delta=5
        )

    def test_defaults_to_30_days_when_expiry_absent(self):
        export = self.make_export(share_token=None, share_mode="private")
        response = self.update(export, {"share_mode": "public"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assert_days_from_now(data["expires_at"], 30)
        export.refresh_from_db()
        self.assertIsNotNone(export.expires_at)

    def test_absent_expiry_keeps_existing(self):
        current = timezone.now() + timedelta(days=3)
        export = self.make_export(expires_at=current)
        response = self.update(export, {"share_mode": "public"})
        self.assertEqual(response.status_code, 200)
        export.refresh_from_db()
        self.assertEqual(export.expires_at, current)

    def test_explicit_days(self):
        export = self.make_export()
        response = self.update(
            export, {"share_mode": "public", "expires_in_days": "7"}
        )
        self.assertEqual(response.status_code, 200)
        self.assert_days_from_now(response.json()["expires_at"], 7)

    def test_never_allowed_for_admin(self):
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

    def test_invalid_expiry_rejected(self):
        export = self.make_export()
        for raw in ("soon", 0, 400):
            with self.subTest(raw=raw):
                response = self.update(
                    export, {"share_mode": "public", "expires_in_days": raw}
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["success"])

    def test_private_clears_expiry(self):
        export = self.make_export(
            expires_at=timezone.now() + timedelta(days=3)
        )
        response = self.update(export, {"share_mode": "private"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["expires_at"])
        export.refresh_from_db()
        self.assertIsNone(export.expires_at)
        self.assertIsNone(export.share_token)
