"""Seed commands create what is missing and leave existing rows alone.

Re-running a ``setup_*_modalities`` command used to ``setattr`` every field of an
existing modality back to its code value and re-link it to the project, silently
reverting whatever an admin had changed.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from common.models import Modality

SEEDS = {
    "maxillo": "create_maxillo_modalities",
    "brain": "setup_brain_modalities",
    "laparoscopy": "setup_laparoscopy_modalities",
    "urology": "setup_urology_modalities",
}


class SeedsKeepAdminEditsTests(TestCase):
    def _seed(self, command):
        call_command(command, stdout=StringIO(), stderr=StringIO())

    def test_rerunning_a_seed_keeps_admin_edits(self):
        for domain, command in SEEDS.items():
            with self.subTest(domain=domain):
                self._seed(command)
                modality = Modality.objects.filter(domain=domain).order_by("id").first()
                self.assertIsNotNone(modality, f"{command} created no {domain} modality")
                projects = list(modality.projects.all()) if hasattr(modality, "projects") else []

                Modality.objects.filter(pk=modality.pk).update(name=f"Renamed by an admin ({domain})")
                for project in projects:
                    project.modalities.remove(modality)

                self._seed(command)

                modality.refresh_from_db()
                self.assertEqual(modality.name, f"Renamed by an admin ({domain})")
                for project in projects:
                    self.assertFalse(project.modalities.filter(pk=modality.pk).exists())
