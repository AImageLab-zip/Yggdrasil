"""Login throttling, session hygiene, and what the health endpoint tells strangers."""

from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings


@override_settings(AXES_ENABLED=True, AXES_FAILURE_LIMIT=3)
class LoginThrottleTests(TestCase):
    def setUp(self):
        User.objects.create_user("throttled", password="right-password")

    def _login(self, password, ip="203.0.113.7"):
        return self.client.post(
            "/login/", {"username": "throttled", "password": password}, REMOTE_ADDR=ip
        )

    def test_repeated_failures_lock_the_username_from_that_address(self):
        for _ in range(3):
            self._login("wrong")
        response = self._login("right-password")
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_lockout_is_per_address(self):
        for _ in range(3):
            self._login("wrong")
        response = self._login("right-password", ip="198.51.100.9")
        self.assertEqual(response.status_code, 302)

    def test_a_success_resets_the_count(self):
        for _ in range(2):
            self._login("wrong")
        self.assertEqual(self._login("right-password").status_code, 302)
        self.client.logout()
        for _ in range(2):
            self._login("wrong")
        self.assertEqual(self._login("right-password").status_code, 302)


class SessionHygieneTests(TestCase):
    def test_expired_sessions_are_purged(self):
        from datetime import timedelta

        from django.contrib.sessions.models import Session
        from django.utils import timezone

        from common.tasks import clear_expired_sessions

        Session.objects.create(session_key="old", session_data="x", expire_date=timezone.now() - timedelta(days=1))
        Session.objects.create(session_key="live", session_data="x", expire_date=timezone.now() + timedelta(days=1))
        clear_expired_sessions()
        self.assertEqual(list(Session.objects.values_list("session_key", flat=True)), ["live"])

    def test_the_purge_is_scheduled_on_the_maintenance_queue(self):
        from django.conf import settings

        entry = settings.CELERY_BEAT_SCHEDULE["clear-expired-sessions-daily"]
        self.assertEqual(entry["task"], "common.tasks.clear_expired_sessions")
        self.assertEqual(entry["options"]["queue"], settings.MAINTENANCE_QUEUE)


class HealthEndpointTests(TestCase):
    def _get(self):
        storage = mock.MagicMock()
        storage._client.list_objects_v2.side_effect = RuntimeError("endpoint http://10.0.0.5:3900 bucket prod-data")
        with mock.patch("maxillo.api_views.health.get_object_storage", return_value=storage):
            return self.client.get("/api/processing/health/")

    def test_anonymous_callers_get_status_only(self):
        body = self._get().json()
        self.assertEqual(body, {"success": True, "status": "degraded"})

    def test_staff_get_the_detail(self):
        self.client.force_login(User.objects.create_user("ops", is_staff=True))
        body = self._get().json()
        self.assertIn("pending_jobs", body)
        self.assertIn("10.0.0.5", body["object_storage_error"])


class KnownHostsTests(TestCase):
    def test_an_explicit_known_hosts_file_is_loaded(self):
        from common.runner.ssh import SlurmSSH

        with override_settings(SLURM_SSH_HOST="login", SLURM_KNOWN_HOSTS="/run/secrets/kh"), \
                mock.patch("paramiko.SSHClient") as client_cls:
            client = client_cls.return_value
            with SlurmSSH.from_settings():
                pass
        client.load_host_keys.assert_called_once_with("/run/secrets/kh")
        client.load_system_host_keys.assert_not_called()
