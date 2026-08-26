"""Tests for the committed Cornerstone bundle's Django-side lookup.

Two jobs. First, that the *real* committed bundle and the manifest agree -- a broken
build would otherwise only surface in a browser. Second, that every degraded case
(no manifest, malformed manifest, unknown surface) renders a console error rather than
raising, because ``patient_detail.html`` must keep rendering the rest of the record.

See docs/cornerstone-roadmap.md, Phase 1.
"""

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.template import Context, Template
from django.test import SimpleTestCase, override_settings

from common import cornerstone_assets


def render_tag(name):
    return Template("{% load cornerstone %}{% cornerstone_entry name %}").render(
        Context({"name": name})
    )


class CommittedBundleTests(SimpleTestCase):
    """The bundle in the repository must be internally consistent."""

    def setUp(self):
        cornerstone_assets.reload()

    def test_manifest_is_present_and_names_a_build(self):
        self.assertIsNotNone(
            cornerstone_assets.get_manifest(),
            "static/vendor/cornerstone/manifest.json is missing or unusable -- "
            "run scripts/build_frontend.sh",
        )
        self.assertTrue(cornerstone_assets.get_build())

    def test_every_advertised_entry_exists_on_disk(self):
        static_dir = Path(settings.STATICFILES_DIRS[0])
        entries = cornerstone_assets.get_entries()
        self.assertTrue(entries, "manifest advertises no entries")
        for name in entries:
            path = cornerstone_assets.entry_static_path(name)
            self.assertIsNotNone(path, name)
            self.assertTrue((static_dir / path).is_file(), f"{name} -> {path}")

    #: The five per-surface bundles. Phases 3, 4, 6, 7 and 10 each own one.
    SURFACE_ENTRIES = [
        "mesh-landmarks",
        "panoramic-cpr",
        "photo-stack",
        "video-annotate",
        "volume-grid",
    ]

    #: Entries that are scaffolding and have a deletion date. ``volume-validation``
    #: is the Phase 3 harness: it is the only place in the tree that vendors NiiVue,
    #: and it goes when the viewer replacement merges.
    TEMPORARY_ENTRIES = ["volume-validation"]

    def test_the_five_surfaces_the_roadmap_names_are_all_present(self):
        # If a rename drops one, the phase that needs it should fail here rather than
        # in a template.
        entries = set(cornerstone_assets.get_entries())
        for name in self.SURFACE_ENTRIES:
            self.assertIn(name, entries)

    def test_the_only_extra_entry_is_the_temporary_phase_3_harness(self):
        """A build entry nobody named is either a typo or scaffolding left behind.

        Splitting this from the check above keeps the surface list strict while
        letting the harness exist: when Phase 3 deletes it, ``TEMPORARY_ENTRIES``
        empties and this test becomes the strict equality it used to be.
        """
        extras = sorted(set(cornerstone_assets.get_entries()) - set(self.SURFACE_ENTRIES))
        self.assertEqual(extras, self.TEMPORARY_ENTRIES)

    def test_the_tag_emits_a_module_script(self):
        # F4: an IIFE bundle loses every web worker, so the tag must never emit a
        # classic script.
        html = render_tag("volume-grid")
        self.assertIn('type="module"', html)
        self.assertIn(cornerstone_assets.get_build(), html)
        self.assertIn("/static/vendor/cornerstone/", html)
        self.assertNotIn("console.error", html)


class DegradedManifestTests(SimpleTestCase):
    """A broken bundle degrades to a console error, never to an exception."""

    def tearDown(self):
        cornerstone_assets.reload()

    def _with_static_dir(self, contents):
        """Point STATICFILES_DIRS at a temp dir, optionally writing a manifest."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        if contents is not None:
            manifest = root / cornerstone_assets.MANIFEST_RELATIVE_PATH
            manifest.parent.mkdir(parents=True)
            manifest.write_text(contents, encoding="utf-8")
        return root

    def test_missing_manifest_yields_no_path_and_a_console_error(self):
        root = self._with_static_dir(None)
        with override_settings(STATICFILES_DIRS=[root], STATIC_ROOT=None):
            cornerstone_assets.reload()
            self.assertIsNone(cornerstone_assets.get_build())
            self.assertIsNone(cornerstone_assets.entry_static_path("volume-grid"))
            html = render_tag("volume-grid")
        self.assertIn("console.error", html)
        self.assertNotIn("<script type=", html)

    def test_malformed_manifest_is_treated_as_absent(self):
        root = self._with_static_dir("{not json at all")
        with override_settings(STATICFILES_DIRS=[root], STATIC_ROOT=None):
            with mock.patch.object(cornerstone_assets.logger, "warning") as warned:
                cornerstone_assets.reload()
                self.assertIsNone(cornerstone_assets.get_build())
            html = render_tag("volume-grid")
        self.assertTrue(warned.called, "a present-but-broken manifest should be logged")
        self.assertIn("console.error", html)

    def test_manifest_without_a_build_id_is_rejected(self):
        root = self._with_static_dir(json.dumps({"entries": ["volume-grid"]}))
        with override_settings(STATICFILES_DIRS=[root], STATIC_ROOT=None):
            cornerstone_assets.reload()
            self.assertIsNone(cornerstone_assets.get_build())
            self.assertEqual(cornerstone_assets.get_entries(), [])

    def test_unknown_entry_name_does_not_fabricate_a_path(self):
        root = self._with_static_dir(
            json.dumps({"build": "deadbeef", "entries": ["volume-grid"]})
        )
        with override_settings(STATICFILES_DIRS=[root], STATIC_ROOT=None):
            cornerstone_assets.reload()
            self.assertEqual(cornerstone_assets.get_build(), "deadbeef")
            self.assertIsNone(cornerstone_assets.entry_static_path("no-such-surface"))
            html = render_tag("no-such-surface")
        self.assertIn("console.error", html)
        self.assertIn("no-such-surface", html)

    def test_a_collected_static_root_is_a_fallback_source(self):
        # In production STATICFILES_DIRS still exists in the image, but the collected
        # tree is the one nginx/whitenoise serves; the lookup must find either.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        manifest = root / cornerstone_assets.MANIFEST_RELATIVE_PATH
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"build": "cafe1234", "entries": ["photo-stack"]}))
        with override_settings(STATICFILES_DIRS=[], STATIC_ROOT=root):
            cornerstone_assets.reload()
            self.assertEqual(cornerstone_assets.get_build(), "cafe1234")
            self.assertEqual(
                cornerstone_assets.entry_static_path("photo-stack"),
                "vendor/cornerstone/cafe1234/app/photo-stack.js",
            )
