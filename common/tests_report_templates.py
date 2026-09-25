"""Report templates: which one applies, what it renders, and what it refuses.

The panel these replace was 367 lines of hardcoded HTML behind an
``{% if ns == 'brain' %}/{% elif ns == 'urology' %}`` ladder. Two properties are worth
more than the rest and have their own tests:

* **no domain branch survives** -- urology having three checklists and laparoscopy none is
  a different number of rows, not a different code path;
* **the shipped content is not overwritten by a deploy** -- once an admin edits a
  template, re-running the seed command leaves it alone.
"""

from io import StringIO

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.template import Context, Template
from django.test import RequestFactory, TestCase

from common import report_templates
from common.models import Project, ReportTemplate, ReportTemplateField


def make_template(domain, modality_slug="", project=None, *, name=None, fields=(), **kwargs):
    template = ReportTemplate.objects.create(
        domain=domain,
        modality_slug=modality_slug,
        project=project,
        name=name or f"{domain} {modality_slug or 'all'}",
        **kwargs,
    )
    for order, field in enumerate(fields):
        ReportTemplateField.objects.create(template=template, order=order, **field)
    return template


class ResolutionOrderTests(TestCase):
    """Narrowest scope wins, in a fixed order, with no domain branch anywhere."""

    def setUp(self):
        self.project = Project.objects.create(name="Uro A", slug="uro-a", domain="urology")
        self.other = Project.objects.create(name="Uro B", slug="uro-b", domain="urology")

    def test_project_and_modality_beats_everything(self):
        make_template("urology", "", None, name="domain catch-all")
        make_template("urology", "mri", None, name="domain mri")
        make_template("urology", "", self.project, name="project catch-all")
        winner = make_template("urology", "mri", self.project, name="project mri")
        found = report_templates.template_for("urology", "mri", self.project)
        self.assertEqual(found, winner)

    def test_falls_back_to_the_domain_template_for_that_modality(self):
        make_template("urology", "", None, name="domain catch-all")
        winner = make_template("urology", "mri", None, name="domain mri")
        self.assertEqual(
            report_templates.template_for("urology", "mri", self.project), winner
        )

    def test_falls_back_to_the_projects_catch_all(self):
        make_template("urology", "", None, name="domain catch-all")
        winner = make_template("urology", "", self.project, name="project catch-all")
        self.assertEqual(
            report_templates.template_for("urology", "confocal", self.project), winner
        )

    def test_falls_back_to_the_domain_catch_all(self):
        winner = make_template("brain", "", None, name="brain")
        self.assertEqual(report_templates.template_for("brain", "", None), winner)

    def test_another_projects_template_is_never_used(self):
        make_template("urology", "mri", self.other, name="someone else's")
        self.assertIsNone(
            report_templates.template_for("urology", "mri", self.project)
        )

    def test_an_inactive_template_does_not_apply(self):
        make_template("brain", "", None, is_active=False)
        self.assertIsNone(report_templates.template_for("brain", "", None))

    def test_a_domain_with_no_template_resolves_to_nothing(self):
        """Laparoscopy's state, and it must be ordinary rather than special-cased."""
        self.assertIsNone(report_templates.template_for("laparoscopy", "", None))
        self.assertEqual(report_templates.templates_for("laparoscopy"), [])


class PanelGroupTests(TestCase):
    def setUp(self):
        self.fields = [{
            "key": "finding", "label_en": "Finding", "label_it": "Reperto",
            "description_en": "What you saw.", "description_it": "Cosa hai visto.",
        }]

    def test_urology_yields_one_group_per_modality_keeping_the_slug(self):
        """The slug is what the urology page filters on; losing it hides every group."""
        for modality in ("mri", "confocal", "wsi"):
            make_template("urology", modality, fields=self.fields)
        groups = report_templates.panel_groups("urology", None, "en")
        self.assertEqual([g["modality_slug"] for g in groups], ["confocal", "mri", "wsi"])

    def test_a_single_checklist_domain_yields_one_unscoped_group(self):
        make_template("brain", "", fields=self.fields)
        groups = report_templates.panel_groups("brain", None, "en")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["modality_slug"], "")

    def test_a_domain_with_no_template_yields_no_groups(self):
        self.assertEqual(report_templates.panel_groups("laparoscopy", None, "en"), [])

    def test_a_per_modality_template_replaces_the_catch_all(self):
        """Showing both would list the same checklist twice in the same panel."""
        make_template("urology", "", fields=self.fields)
        make_template("urology", "mri", fields=self.fields)
        groups = report_templates.panel_groups("urology", None, "en")
        self.assertEqual([g["modality_slug"] for g in groups], ["mri"])

    def test_labels_resolve_into_the_requested_language(self):
        make_template("brain", "", fields=self.fields)
        groups = report_templates.panel_groups("brain", None, "it")
        self.assertEqual(groups[0]["fields"][0]["label"], "Reperto")

    def test_a_missing_translation_falls_back_to_english(self):
        """A half-translated template must render; an empty <summary> cannot be opened."""
        make_template("brain", "", fields=self.fields)
        groups = report_templates.panel_groups("brain", None, "de")
        self.assertEqual(groups[0]["fields"][0]["label"], "Finding")
        self.assertEqual(groups[0]["fields"][0]["description"], "What you saw.")

    def test_an_unknown_language_falls_back_to_the_default(self):
        self.assertEqual(report_templates.normalize_language("zz"), "it")
        self.assertEqual(report_templates.normalize_language(None), "it")

    def test_modality_slugs_report_a_wildcard_for_a_catch_all(self):
        make_template("brain", "", fields=self.fields)
        self.assertEqual(report_templates.modality_slugs_with_templates("brain"), ["*"])

    def test_modality_slugs_list_the_modalities_that_have_one(self):
        make_template("urology", "mri", fields=self.fields)
        make_template("urology", "wsi", fields=self.fields)
        self.assertEqual(
            sorted(report_templates.modality_slugs_with_templates("urology")),
            ["mri", "wsi"],
        )


class PanelRenderingTests(TestCase):
    """What the template tag puts on the page."""

    def _render(self, namespace, language="en"):
        request = RequestFactory().get("/")
        request.resolver_match = type("M", (), {"namespace": namespace})()
        rendered = Template(
            "{% load report_templates %}{% report_template_panel %}"
        ).render(Context({"request": request, "report_language": language}))
        return rendered

    def test_a_domain_with_no_template_renders_nothing_at_all(self):
        """No heading, no empty card, no language buttons -- the guard this replaced."""
        self.assertEqual(self._render("laparoscopy").strip(), "")

    def test_all_three_languages_are_rendered_and_two_are_hidden(self):
        """The toggle switches classes; rendering one language would break it."""
        make_template("brain", "", fields=[{
            "key": "finding", "label_en": "Finding", "label_it": "Reperto",
            "label_de": "Befund",
        }])
        html = self._render("brain", "it")
        for language in ("en", "it", "de"):
            self.assertIn(f"report-fields-{language}", html)
        self.assertIn('report-fields report-fields-en d-none', html)
        self.assertIn("Reperto", html)
        self.assertIn("Befund", html)

    def test_the_urology_modality_attribute_survives(self):
        """templates/urology/patient_detail_content.html filters on this attribute."""
        make_template("urology", "mri", fields=[{"key": "f", "label_en": "F"}])
        self.assertIn('data-urology-modality="mri"', self._render("urology"))

    def test_admin_entered_html_is_escaped(self):
        """The one untrusted-input path this panel has."""
        make_template("brain", "", fields=[{
            "key": "x", "label_en": "<script>alert(1)</script>",
            "description_en": "<img src=x onerror=alert(1)>",
        }])
        html = self._render("brain")
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;", html)

    def test_the_rendered_panel_contains_no_domain_conditional(self):
        """The 367-line `{% if ns == ... %}` ladder is gone and must not come back.

        Comments are stripped first: the file's own header explains what it replaced, and
        an assertion that forbade naming the old design would be a rule against
        documenting it.
        """
        import re

        source = "templates/common/sections/report_template_section.html"
        with open(source, encoding="utf-8") as handle:
            body = handle.read()
        logic = re.sub(
            r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", "", body, flags=re.S
        )
        self.assertNotIn("ns == 'brain'", logic)
        self.assertNotIn("ns == 'urology'", logic)
        self.assertNotIn("ns != 'laparoscopy'", logic)
        # And the include it replaced no longer guards on the domain either.
        with open(
            "templates/common/sections/vocal_caption_section.html", encoding="utf-8"
        ) as handle:
            caller = re.sub(
                r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", "", handle.read(),
                flags=re.S,
            )
        self.assertNotIn("ns != 'laparoscopy'", caller)
        self.assertIn("report_template_panel", caller)


class ScopeConstraintTests(TestCase):
    """The uniqueness rule, and the MySQL trap it is shaped around."""

    def test_two_active_domain_wide_templates_are_refused(self):
        """A nullable FK in the index would let this through: MySQL treats NULLs as
        distinct, so the constraint names ``project_key`` instead."""
        make_template("brain", "")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_template("brain", "", name="a second one")

    def test_an_inactive_duplicate_is_allowed(self):
        """Superseding a template without deleting it has to be possible."""
        make_template("brain", "")
        make_template("brain", "", name="retired", is_active=False)
        self.assertEqual(ReportTemplate.objects.filter(domain="brain").count(), 2)

    def test_a_project_template_coexists_with_the_domain_default(self):
        project = Project.objects.create(name="B", slug="b", domain="brain")
        make_template("brain", "")
        make_template("brain", "", project, name="project one")
        self.assertEqual(ReportTemplate.objects.filter(domain="brain").count(), 2)

    def test_resolution_is_single_valued_even_without_the_constraint(self):
        """``template_for`` must not raise MultipleObjectsReturned in front of a user."""
        first = make_template("brain", "")
        ReportTemplate.objects.filter(pk=first.pk).update(active_slot=None)
        second = make_template("brain", "", name="second")
        found = report_templates.template_for("brain", "")
        self.assertIn(found, (first, second))


class SeedCommandTests(TestCase):
    def test_it_creates_the_shipped_templates(self):
        call_command("seed_report_templates", stdout=StringIO())
        self.assertEqual(
            ReportTemplate.objects.filter(domain="brain").count(), 1
        )
        self.assertEqual(
            ReportTemplate.objects.filter(domain="urology").count(), 3
        )
        # Transcribed from the hardcoded panel: 16 brain fields, 11 maxillofacial,
        # and 10/6/7 for urology's three modalities.
        self.assertEqual(
            ReportTemplate.objects.get(domain="brain").fields.count(), 16
        )
        self.assertEqual(
            ReportTemplate.objects.get(domain="maxillo").fields.count(), 11
        )
        self.assertEqual(
            ReportTemplate.objects.get(domain="urology", modality_slug="mri")
            .fields.count(),
            10,
        )

    def test_laparoscopy_ships_no_template(self):
        call_command("seed_report_templates", stdout=StringIO())
        self.assertEqual(ReportTemplate.objects.filter(domain="laparoscopy").count(), 0)

    def test_re_running_does_not_overwrite_an_admins_edits(self):
        """The whole point of the feature: a deploy must not revert clinical wording."""
        call_command("seed_report_templates", stdout=StringIO())
        field = ReportTemplate.objects.get(domain="brain").fields.first()
        field.label_it = "Una traduzione corretta a mano"
        field.save()

        call_command("seed_report_templates", stdout=StringIO())
        field.refresh_from_db()
        self.assertEqual(field.label_it, "Una traduzione corretta a mano")

    def test_force_resets_to_the_shipped_content(self):
        call_command("seed_report_templates", stdout=StringIO())
        template = ReportTemplate.objects.get(domain="brain")
        template.fields.all().delete()

        call_command("seed_report_templates", "--force", stdout=StringIO())
        template.refresh_from_db()
        self.assertEqual(template.fields.count(), 16)

    def test_dry_run_writes_nothing(self):
        call_command("seed_report_templates", "--dry-run", stdout=StringIO())
        self.assertEqual(ReportTemplate.objects.count(), 0)

    def test_the_seeded_content_has_no_html_entities_left_in_it(self):
        """The extraction came out of HTML; an unescaped ``&amp;`` would render literally."""
        call_command("seed_report_templates", stdout=StringIO())
        for field in ReportTemplateField.objects.all():
            for value in (field.label_en, field.label_it, field.label_de,
                          field.description_en, field.description_it, field.description_de):
                self.assertNotIn("&amp;", value)
                self.assertNotIn("&lt;", value)
                self.assertNotIn("&gt;", value)

    def test_every_seeded_field_has_a_key_and_an_english_label(self):
        call_command("seed_report_templates", stdout=StringIO())
        for field in ReportTemplateField.objects.all():
            self.assertTrue(field.key)
            self.assertTrue(field.label_en)


class ModalitySlugNormalizationTests(TestCase):
    """A caption carries ``urology-mri``; the checklist is filed under ``mri``.

    The urology page strips the domain prefix in JS before matching
    ``data-urology-modality``, and the hardcoded panel was written to match. Without the
    same normalization server-side, structuring a urology caption would find no template
    and the button would be permanently unavailable -- with nothing to point at.
    """

    def test_a_prefixed_slug_finds_the_bare_template(self):
        winner = make_template("urology", "mri", fields=[{"key": "f", "label_en": "F"}])
        self.assertEqual(report_templates.template_for("urology", "urology-mri"), winner)

    def test_a_bare_slug_still_works(self):
        winner = make_template("urology", "mri", fields=[{"key": "f", "label_en": "F"}])
        self.assertEqual(report_templates.template_for("urology", "mri"), winner)

    def test_a_domain_whose_slugs_are_not_prefixed_is_unaffected(self):
        winner = make_template("maxillo", "cbct", fields=[{"key": "f", "label_en": "F"}])
        self.assertEqual(report_templates.template_for("maxillo", "cbct"), winner)

    def test_only_the_domains_own_prefix_is_stripped(self):
        """``brain-mri`` in urology is not ``mri``: it is a slug that has no template."""
        make_template("urology", "mri", fields=[{"key": "f", "label_en": "F"}])
        self.assertIsNone(report_templates.template_for("urology", "brain-mri"))
