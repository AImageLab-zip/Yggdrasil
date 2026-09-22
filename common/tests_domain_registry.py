"""Every registered domain has what the shared code assumes it has.

Adding a domain touched ~30 shared files and still missed several (the urology gaps
fixed alongside this test), because nothing listed what "a domain" must provide.
These tests loop over ``common.domains.DOMAINS``: a new domain is checked the moment
it is registered, and a hand-written domain list in ``common/`` is ratcheted down.
"""

import ast
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.test import SimpleTestCase
from django.urls import get_resolver

from common.domain_models import REQUIRED_MODELS
from common.domains import DOMAIN_CHOICES, DOMAINS, fk_fields_for

#: URL names every domain routes today. Shared views, templates and the viewer JS
#: build these per namespace (``/${namespace}/api/...``, ``ns|add:":name"``), so a
#: domain without one fails at runtime with a NoReverseMatch or a 404.
SHARED_ROUTE_NAMES = frozenset({
    "add_patient_tag", "admin_control_panel", "api_measurements_state",
    "api_save_measurements", "api_serve_file", "api_serve_file_named",
    "bulk_delete_patients", "bulk_rerun_processing", "create_folder", "delete_folder",
    "delete_patient", "delete_voice_caption", "edit_voice_caption_transcription",
    "export_delete", "export_download", "export_list", "export_new", "export_preview",
    "export_share_update", "export_shared_download", "export_shared_landing",
    "export_status", "export_stop", "folder_stats", "home", "move_patients_to_folder",
    "patient_detail", "patient_list", "remove_patient_tag", "rename_folder",
    "rerun_processing", "select_project", "update_patient_name",
    "update_voice_caption_modality", "upload_patient", "upload_text_caption",
    "user_profile", "user_profile_by_username",
})

#: Comparisons against a domain slug literal in non-test ``common/`` code at the time
#: this was written. The number may only go down: domain behaviour belongs in the
#: registry or the domain app, not in ``if domain == "..."`` branches in core.
MAX_DOMAIN_SLUG_COMPARISONS_IN_COMMON = 18


class DomainRegistryCompletenessTests(SimpleTestCase):
    def test_the_registry_and_its_labels_agree(self):
        self.assertEqual({slug for slug, _ in DOMAIN_CHOICES}, set(DOMAINS))
        for domain in DOMAINS:
            with self.subTest(domain=domain):
                self.assertTrue(apps.is_installed(domain), f"{domain} is not an installed app")

    def test_every_domain_declares_the_models_shared_views_resolve(self):
        for domain in sorted(DOMAINS):
            for name in REQUIRED_MODELS:
                with self.subTest(domain=domain, model=name):
                    self.assertIsNotNone(apps.get_model(domain, name))

    def test_shared_tables_carry_this_domains_fk_columns(self):
        shared = {
            "common.Job": True,
            "common.ProcessingJob": True,
            "common.FileRegistry": True,
            "annotations.AnnotationSet": False,  # patient FK only
        }
        for domain in sorted(DOMAINS):
            patient_fk, caption_fk = fk_fields_for(domain)
            for label, has_caption in shared.items():
                model = apps.get_model(label)
                with self.subTest(domain=domain, table=label, fk=patient_fk):
                    field = model._meta.get_field(patient_fk)
                    self.assertEqual(field.related_model, apps.get_model(domain, "Patient"))
                if has_caption:
                    with self.subTest(domain=domain, table=label, fk=caption_fk):
                        field = model._meta.get_field(caption_fk)
                        self.assertEqual(field.related_model, apps.get_model(domain, "VoiceCaption"))

    def test_every_domain_routes_the_shared_surface(self):
        resolver = get_resolver()
        for domain in sorted(DOMAINS):
            with self.subTest(domain=domain):
                self.assertIn(domain, resolver.namespace_dict, f"no URL namespace '{domain}'")
                routed = {
                    name for name in resolver.namespace_dict[domain][1].reverse_dict.keys()
                    if isinstance(name, str)
                }
                self.assertEqual(sorted(SHARED_ROUTE_NAMES - routed), [])

    def test_domain_slug_comparisons_in_common_do_not_grow(self):
        found = []
        root = Path(settings.BASE_DIR) / "common"
        for path in sorted(root.rglob("*.py")):
            if "migrations" in path.parts or path.name.startswith("tests"):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Compare):
                    continue
                for operand in (node.left, *node.comparators):
                    literals = operand.elts if isinstance(operand, (ast.Tuple, ast.List, ast.Set)) else [operand]
                    if any(isinstance(e, ast.Constant) and e.value in DOMAINS for e in literals):
                        found.append(f"{path.relative_to(settings.BASE_DIR)}:{node.lineno}")
        self.assertLessEqual(
            len(found),
            MAX_DOMAIN_SLUG_COMPARISONS_IN_COMMON,
            "new domain-slug comparison in common/ -- use the registry "
            f"(common.domains) or the domain app instead:\n" + "\n".join(found),
        )
