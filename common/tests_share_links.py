"""Who may publish an export link, how long it lives, and what the logs keep."""

import json
import logging
from datetime import timedelta

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from common.models import Project, ProjectAccess
from maxillo.models import Export
from yggdrasil.log_filters import RedactShareTokens


class SharePublishingTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Share", slug="share-links", domain="maxillo")
        self.annotator = User.objects.create_user("share-annotator")
        self.admin = User.objects.create_user("share-project-admin")
        ProjectAccess.objects.create(user=self.annotator, project=self.project, role="annotator")
        ProjectAccess.objects.create(user=self.admin, project=self.project, role="admin")

    def _export(self, owner):
        return Export.objects.create(
            user=owner, status="completed", query_params={"project_id": self.project.id},
            file_path="exports/x.zip",
        )

    def _share(self, user, export, **body):
        self.client.force_login(user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()
        return self.client.post(
            reverse("maxillo:export_share_update", args=[export.id]),
            data=json.dumps(body), content_type="application/json",
        )

    def test_an_annotator_cannot_publish_their_own_export(self):
        export = self._export(self.annotator)
        for mode in ("public", "authenticated"):
            with self.subTest(mode=mode):
                response = self._share(self.annotator, export, share_mode=mode)
                self.assertEqual(response.status_code, 403)
                export.refresh_from_db()
                self.assertEqual(export.share_mode, "private")
                self.assertIsNone(export.share_token)

    def test_a_project_admin_can_and_the_link_expires(self):
        export = self._export(self.admin)
        response = self._share(self.admin, export, share_mode="public")
        self.assertEqual(response.status_code, 200)
        export.refresh_from_db()
        self.assertEqual(export.share_mode, "public")
        self.assertIsNotNone(export.expires_at)

    def test_a_public_link_cannot_be_made_permanent(self):
        export = self._export(self.admin)
        response = self._share(self.admin, export, share_mode="public", expires_in_days="never")
        self.assertEqual(response.status_code, 400)

    def test_revoking_stays_open_to_the_owner(self):
        export = self._export(self.annotator)
        export.share_mode, export.share_token = "public", "tok"
        export.save()
        response = self._share(self.annotator, export, share_mode="private")
        self.assertEqual(response.status_code, 200)
        export.refresh_from_db()
        self.assertEqual(export.share_mode, "private")


class LegacyLinkMigrationTests(TestCase):
    def test_never_expiring_public_links_get_an_expiry(self):
        import importlib

        migration = importlib.import_module("common.migrations.0058_expire_legacy_share_links")
        owner = User.objects.create_user("legacy-owner")
        public = Export.objects.create(user=owner, share_mode="public", share_token="a")
        logged_in = Export.objects.create(user=owner, share_mode="authenticated", share_token="b")
        dated = timezone.now() + timedelta(days=3)
        already = Export.objects.create(user=owner, share_mode="public", share_token="c", expires_at=dated)

        migration.expire_legacy_links(django_apps, None)

        for export in (public, logged_in, already):
            export.refresh_from_db()
        self.assertIsNotNone(public.expires_at)
        self.assertGreater(public.expires_at, timezone.now() + timedelta(days=29))
        self.assertIsNone(logged_in.expires_at)
        self.assertEqual(already.expires_at, dated)


class ShareTokenRedactionTests(SimpleTestCase):
    def _record(self, msg, *args):
        return logging.LogRecord("t", logging.WARNING, __file__, 1, msg, args, None)

    def test_tokens_in_paths_are_redacted(self):
        record = self._record("Not Found: %s", "/brain/export/shared/SeCrEt-Tok_en/download/")
        RedactShareTokens().filter(record)
        self.assertEqual(record.getMessage(), "Not Found: /brain/export/shared/<redacted>/download/")

    def test_other_messages_are_untouched(self):
        record = self._record("Request: GET %s", "/maxillo/patients/")
        RedactShareTokens().filter(record)
        self.assertEqual(record.args, ("/maxillo/patients/",))
