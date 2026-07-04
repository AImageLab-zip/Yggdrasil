"""Tests for the shared export-link expiry rules (common.export_share).

Pure-function tests only; view-level enforcement is covered per domain in
maxillo/tests_export_share.py and brain/tests_export_share.py.
"""

from datetime import timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase
from django.utils import timezone

from common.export_share import (
    SHARE_EXPIRY_DEFAULT_DAYS,
    SHARE_EXPIRY_MAX_DAYS,
    is_share_expired,
    resolve_share_expiry,
)


class IsShareExpiredTests(SimpleTestCase):
    def test_null_expiry_never_expires(self):
        export = SimpleNamespace(expires_at=None)
        self.assertFalse(is_share_expired(export))

    def test_future_expiry_is_not_expired(self):
        export = SimpleNamespace(expires_at=timezone.now() + timedelta(hours=1))
        self.assertFalse(is_share_expired(export))

    def test_past_expiry_is_expired(self):
        export = SimpleNamespace(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertTrue(is_share_expired(export))

    def test_object_without_field_never_expires(self):
        self.assertFalse(is_share_expired(object()))


class ResolveShareExpiryTests(SimpleTestCase):
    def assert_days_from_now(self, value, days):
        expected = timezone.now() + timedelta(days=days)
        self.assertAlmostEqual(
            value.timestamp(), expected.timestamp(), delta=5,
        )

    def test_absent_with_no_current_applies_default(self):
        for raw in (None, ""):
            with self.subTest(raw=raw):
                expires_at, error = resolve_share_expiry(
                    raw, current=None, can_set_never=False
                )
                self.assertIsNone(error)
                self.assert_days_from_now(expires_at, SHARE_EXPIRY_DEFAULT_DAYS)

    def test_absent_keeps_existing_expiry(self):
        current = timezone.now() + timedelta(days=3)
        expires_at, error = resolve_share_expiry(
            None, current=current, can_set_never=False
        )
        self.assertIsNone(error)
        self.assertEqual(expires_at, current)

    def test_integer_days(self):
        expires_at, error = resolve_share_expiry(7, current=None, can_set_never=False)
        self.assertIsNone(error)
        self.assert_days_from_now(expires_at, 7)

    def test_string_days(self):
        expires_at, error = resolve_share_expiry(
            "90", current=None, can_set_never=False
        )
        self.assertIsNone(error)
        self.assert_days_from_now(expires_at, 90)

    def test_never_requires_permission(self):
        expires_at, error = resolve_share_expiry(
            "never", current=None, can_set_never=False
        )
        self.assertIsNone(expires_at)
        self.assertIn("staff or project admins", error)

    def test_never_allowed_is_case_insensitive(self):
        for raw in ("never", "NEVER", " Never "):
            with self.subTest(raw=raw):
                expires_at, error = resolve_share_expiry(
                    raw, current=None, can_set_never=True
                )
                self.assertIsNone(error)
                self.assertIsNone(expires_at)

    def test_out_of_range_days_rejected(self):
        for raw in (0, -1, SHARE_EXPIRY_MAX_DAYS + 1):
            with self.subTest(raw=raw):
                expires_at, error = resolve_share_expiry(
                    raw, current=None, can_set_never=True
                )
                self.assertIsNone(expires_at)
                self.assertIn(f"between 1 and {SHARE_EXPIRY_MAX_DAYS}", error)

    def test_non_numeric_rejected(self):
        for raw in ("soon", [7], {"days": 7}):
            with self.subTest(raw=raw):
                expires_at, error = resolve_share_expiry(
                    raw, current=None, can_set_never=True
                )
                self.assertIsNone(expires_at)
                self.assertIn("number of days or 'never'", error)
