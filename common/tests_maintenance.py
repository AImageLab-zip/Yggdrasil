import gzip
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from common.models import SystemCheck
from common.tasks import backup_database, select_backups_to_delete


def _key(ts):
    return f"backups/mysql/ygg_{ts.strftime('%Y%m%d_%H%M%S')}.sql.gz"


class BackupRetentionTests(SimpleTestCase):
    def test_keeps_recent_dailies(self):
        now = datetime(2026, 7, 3, 3, 0, tzinfo=dt_timezone.utc)
        keys = [_key(now - timedelta(days=i)) for i in range(10)]
        deleted = select_backups_to_delete(keys, keep_daily=14, keep_weekly=8)
        self.assertEqual(deleted, [])

    def test_deletes_beyond_daily_and_weekly_windows(self):
        now = datetime(2026, 7, 3, 3, 0, tzinfo=dt_timezone.utc)
        keys = [_key(now - timedelta(days=i)) for i in range(60)]
        deleted = select_backups_to_delete(keys, keep_daily=14, keep_weekly=4)
        kept = [k for k in keys if k not in deleted]
        # 14 dailies + at most 4 weekly representatives
        self.assertEqual(len(kept), 18)
        # The newest 14 are always kept
        for k in keys[:14]:
            self.assertIn(k, kept)

    def test_keeps_one_per_week_beyond_daily_window(self):
        now = datetime(2026, 7, 3, 3, 0, tzinfo=dt_timezone.utc)
        keys = [_key(now - timedelta(days=i)) for i in range(28)]
        deleted = select_backups_to_delete(keys, keep_daily=7, keep_weekly=8)
        # Everything older than the 7 dailies collapses to one key per ISO week.
        older_kept = [k for k in keys[7:] if k not in deleted]
        self.assertLessEqual(len(older_kept), 4)  # 21 days ≈ 3-4 ISO weeks

    def test_ignores_foreign_keys_in_prefix(self):
        keys = ["backups/mysql/README.txt", "backups/mysql/not-a-dump.sql.gz"]
        self.assertEqual(
            select_backups_to_delete(keys, keep_daily=1, keep_weekly=1), []
        )


class BackupTaskTests(TestCase):
    def _fake_dump(self, dump_path):
        with gzip.open(dump_path, "wb") as fh:
            fh.write(b"-- fake dump\n" * 100)

    def test_successful_backup_records_ok_check(self):
        storage = mock.MagicMock()
        storage.list_keys.return_value = iter([])
        with mock.patch("common.tasks._run_mysqldump", side_effect=self._fake_dump), \
                mock.patch("common.tasks.get_object_storage", return_value=storage):
            result = backup_database()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(storage.upload_file.call_count, 1)
        _, kwargs = storage.upload_file.call_args
        self.assertTrue(kwargs["key"].startswith("backups/mysql/"))
        self.assertTrue(kwargs["key"].endswith(".sql.gz"))

        check = SystemCheck.objects.get(name="database_backup")
        self.assertEqual(check.status, "ok")
        self.assertGreater(check.details["uncompressed_bytes"], 0)

    def test_failed_backup_records_fail_check(self):
        with mock.patch(
            "common.tasks._run_mysqldump", side_effect=RuntimeError("dump broke")
        ), mock.patch("common.tasks.get_object_storage"):
            result = backup_database()

        self.assertEqual(result["status"], "fail")
        check = SystemCheck.objects.get(name="database_backup")
        self.assertEqual(check.status, "fail")
        self.assertIn("dump broke", check.details["error"])


_STORAGE_UP = {"status": "up", "label": "Up", "message": "ok"}
_STORAGE_DOWN = {"status": "down", "label": "Down", "message": "kaput"}


class HealthzTests(TestCase):
    def test_healthy_returns_200_without_details(self):
        with mock.patch(
            "common.views._object_storage_health", return_value=_STORAGE_UP
        ):
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_degraded_returns_503(self):
        with mock.patch(
            "common.views._object_storage_health", return_value=_STORAGE_DOWN
        ):
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})


class StatusPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            "staffer", password="pw", is_staff=True
        )
        cls.plain = User.objects.create_user("plain", password="pw")

    def _get(self):
        with mock.patch(
            "common.views._object_storage_health", return_value=_STORAGE_UP
        ):
            return self.client.get("/status/")

    def test_anonymous_is_redirected_to_login(self):
        response = self._get()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_non_staff_cannot_view(self):
        self.client.login(username="plain", password="pw")
        response = self._get()
        self.assertEqual(response.status_code, 302)

    def test_staff_sees_status_with_backup_warning_when_none_recorded(self):
        self.client.login(username="staffer", password="pw")
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No successful backup recorded yet")

    def test_stale_backup_warns(self):
        SystemCheck.objects.create(name="database_backup", status="ok")
        SystemCheck.objects.filter(name="database_backup").update(
            ran_at=timezone.now() - timedelta(hours=30)
        )
        self.client.login(username="staffer", password="pw")
        response = self._get()
        self.assertContains(response, "limit 26h")

    def test_fresh_backup_is_ok(self):
        SystemCheck.objects.create(name="database_backup", status="ok")
        self.client.login(username="staffer", password="pw")
        response = self._get()
        self.assertContains(response, "Last successful backup at")


class AdminControlPanelAccessTests(TestCase):
    def test_anonymous_is_redirected(self):
        response = self.client.get("/admin/control-panel/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)
