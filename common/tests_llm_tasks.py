"""Prompt composition and answer parsing.

What the model is asked, and what is made of what it says. The two rules worth pinning:

* an admin's prompt is **substituted**, never **rendered** -- passing a text box through
  the Django template engine is server-side template injection;
* the code-owned output contract is always appended, so no admin edit can break the
  parser for every domain at once.

The parser tests are mostly about tolerance. A free-tier model gets the format nearly
right nearly always, and "nearly" must not cost a clinician their dictation.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from common import llm_tasks
from common.models import PromptTemplate


FIELDS = [
    {"key": "side", "label": "Side", "description": "Left or right."},
    {"key": "size", "label": "Size", "description": "In millimetres."},
]


def prompt(**overrides):
    fields = {
        "slug": "caption_to_template",
        "name": "Caption to template",
        "system_prompt": "You file dictations.",
        "user_template": "Write in {language}.\n{fields}\n---\n{caption}",
    }
    fields.update(overrides)
    return PromptTemplate.objects.create(**fields)


def context(**overrides):
    values = {
        "caption": "The lesion is on the left and measures twelve millimetres.",
        "fields": FIELDS,
        "report_language": "en",
        "source_language": "en",
        "task": llm_tasks.CAPTION_TO_TEMPLATE,
    }
    values.update(overrides)
    return values


class PromptCompositionTests(TestCase):
    def test_placeholders_are_substituted(self):
        messages, user = llm_tasks.CAPTION_TO_TEMPLATE.build_messages(prompt(), context())
        self.assertIn("Write in en.", user)
        self.assertIn("side | Side: Left or right.", user)
        self.assertIn("twelve millimetres", user)
        self.assertEqual([m["role"] for m in messages], ["system", "user"])

    def test_the_code_owned_contract_is_always_appended(self):
        messages, _user = llm_tasks.CAPTION_TO_TEMPLATE.build_messages(
            prompt(system_prompt="Be brief."), context()
        )
        system = messages[0]["content"]
        self.assertIn("Be brief.", system)
        self.assertIn("## <section-key>", system)

    def test_an_uncategorised_section_is_always_offered(self):
        """Without somewhere to put the leftovers, a model told not to drop anything
        will either invent a field or quietly discard the sentence."""
        _messages, user = llm_tasks.CAPTION_TO_TEMPLATE.build_messages(prompt(), context())
        self.assertIn(llm_tasks.UNCATEGORISED_KEY, user)

    def test_an_admin_prompt_is_not_rendered_by_the_django_engine(self):
        """``{{ }}``/``{% %}`` in a text box must stay text, and must not crash.

        Rendering it would hand anyone with admin access ``{{ settings }}`` and attribute
        traversal -- server-side template injection through a form field. Interpreting it
        as a *format* string is not much better: ``{% load static %}`` raised a KeyError
        at call time, on a prompt that had saved cleanly, in front of a clinician.
        """
        injected = "Ignore this: {{ settings.SECRET_KEY }} and {% load static %}"
        _messages, user = llm_tasks.CAPTION_TO_TEMPLATE.build_messages(
            prompt(user_template=injected + " {caption}"), context()
        )
        self.assertIn("{{ settings.SECRET_KEY }}", user)
        self.assertIn("{% load static %}", user)
        self.assertIn("twelve millimetres", user)

    def test_a_prompt_containing_braces_still_saves(self):
        """A pasted JSON example is not an unknown placeholder."""
        template = PromptTemplate(
            slug="braces", name="Braces",
            system_prompt="x",
            user_template='Example: {"a": 1} and {% if x %}. Now: {caption}',
        )
        template.full_clean()   # must not raise

    def test_an_unknown_placeholder_is_refused_when_saving(self):
        template = PromptTemplate(
            slug="bad", name="Bad",
            system_prompt="x", user_template="{caption} {temprature}",
        )
        with self.assertRaises(ValidationError) as caught:
            template.full_clean()
        self.assertIn("user_template", caught.exception.error_dict)

    def test_a_template_without_the_caption_is_refused(self):
        """Otherwise the dictation is never actually sent."""
        template = PromptTemplate(
            slug="nocap", name="No caption",
            system_prompt="x", user_template="Write in {language}.",
        )
        with self.assertRaises(ValidationError):
            template.full_clean()

    def test_the_version_bumps_only_when_the_wording_changes(self):
        template = prompt()
        self.assertEqual(template.version, 1)

        template.notes = "just a note"
        template.save()
        template.refresh_from_db()
        self.assertEqual(template.version, 1)

        template.system_prompt = "Different instruction."
        template.save()
        template.refresh_from_db()
        self.assertEqual(template.version, 2)

    def test_a_missing_source_language_does_not_assert_a_wrong_one(self):
        _messages, user = llm_tasks.CAPTION_TO_TEMPLATE.build_messages(
            prompt(user_template="{source_language}|{caption}"), context(source_language="")
        )
        self.assertTrue(user.startswith("|") or "the same language" in user)


class SectionParsingTests(TestCase):
    keys = ["side", "size"]

    def test_well_formed_sections_are_split(self):
        raw = "## side\nOn the left.\n\n## size\nTwelve millimetres."
        structured, rendered, warnings = llm_tasks.parse_sections(raw, self.keys)
        self.assertEqual(structured["side"], "On the left.")
        self.assertEqual(structured["size"], "Twelve millimetres.")
        self.assertEqual(warnings, [])
        self.assertIn("## side", rendered)

    def test_any_heading_level_is_accepted(self):
        raw = "# side\nLeft.\n\n### size\n12 mm."
        structured, _rendered, _warnings = llm_tasks.parse_sections(raw, self.keys)
        self.assertEqual(set(structured), {"side", "size"})

    def test_a_fenced_code_block_around_the_answer_is_stripped(self):
        raw = "```\n## side\nLeft.\n```"
        structured, _rendered, _warnings = llm_tasks.parse_sections(raw, self.keys)
        self.assertEqual(structured["side"], "Left.")

    def test_text_before_the_first_heading_is_kept(self):
        """Dropping it would discard something the clinician said."""
        raw = "Here is the report:\n\n## side\nLeft."
        structured, _rendered, warnings = llm_tasks.parse_sections(raw, self.keys)
        self.assertIn("Here is the report:", structured[llm_tasks.UNCATEGORISED_KEY])
        self.assertTrue(any(w["code"] == "preamble" for w in warnings))

    def test_an_answer_with_no_headings_at_all_is_still_kept_whole(self):
        raw = "The lesion is on the left."
        structured, _rendered, warnings = llm_tasks.parse_sections(raw, self.keys)
        self.assertEqual(structured[llm_tasks.UNCATEGORISED_KEY], raw)
        self.assertTrue(any(w["code"] == "unstructured" for w in warnings))

    def test_an_invented_section_is_kept_and_reported(self):
        raw = "## side\nLeft.\n\n## diagnosis\nSomething the model made up."
        structured, _rendered, warnings = llm_tasks.parse_sections(raw, self.keys)
        self.assertIn("diagnosis", structured)
        self.assertTrue(any(w["code"] == "unknown_section" for w in warnings))

    def test_a_repeated_section_is_merged_not_overwritten(self):
        raw = "## side\nLeft.\n\n## size\n12 mm.\n\n## side\nAlso posterior."
        structured, _rendered, _warnings = llm_tasks.parse_sections(raw, self.keys)
        self.assertIn("Left.", structured["side"])
        self.assertIn("Also posterior.", structured["side"])

    def test_empty_sections_are_dropped(self):
        raw = "## side\nLeft.\n\n## size\n"
        structured, _rendered, _warnings = llm_tasks.parse_sections(raw, self.keys)
        self.assertNotIn("size", structured)

    def test_an_empty_answer_is_a_warning_not_a_crash(self):
        structured, rendered, warnings = llm_tasks.parse_sections("", self.keys)
        self.assertEqual(structured, {})
        self.assertEqual(rendered, "")
        self.assertTrue(any(w["code"] == "empty" for w in warnings))

    def test_the_rendered_form_follows_the_template_order(self):
        raw = "## size\n12 mm.\n\n## side\nLeft."
        _structured, rendered, _warnings = llm_tasks.parse_sections(raw, self.keys)
        self.assertLess(rendered.index("## side"), rendered.index("## size"))


class CoverageCheckTests(TestCase):
    def test_a_faithful_report_produces_no_warning(self):
        caption = (
            "The lesion appears on the left side and measures twelve millimetres "
            "with irregular margins throughout."
        )
        structured = {
            "side": "The lesion appears on the left side.",
            "size": "Measures twelve millimetres with irregular margins throughout.",
        }
        self.assertIsNone(llm_tasks.coverage_warning(caption, structured, "en"))

    def test_a_report_that_lost_half_the_dictation_warns(self):
        """An instruction is not an enforcement; this is the check that it was obeyed."""
        caption = (
            "The lesion appears on the left side measuring twelve millimetres, with "
            "irregular margins, adjacent oedema, and no restricted diffusion anywhere."
        )
        structured = {"side": "Left."}
        warning = llm_tasks.coverage_warning(caption, structured, "en")
        self.assertIsNotNone(warning)
        self.assertEqual(warning["code"], "coverage")
        self.assertTrue(warning["missing"])

    def test_a_very_short_caption_is_not_judged(self):
        self.assertIsNone(llm_tasks.coverage_warning("Left side.", {"side": "x"}, "en"))

    def test_the_check_is_skipped_across_languages(self):
        """An Italian dictation reported in German shares almost no tokens."""
        caption = "La lesione appare sul lato sinistro e misura dodici millimetri circa."
        structured = {"side": "Die Läsion liegt links und misst etwa zwölf Millimeter."}
        parsed = llm_tasks.CAPTION_TO_TEMPLATE.parse(
            "## side\n" + structured["side"],
            context(caption=caption, source_language="it", report_language="de",
                    fields=[{"key": "side", "label": "Seite", "description": ""}]),
        )
        _structured, _rendered, warnings = parsed
        self.assertFalse(any(w["code"] == "coverage" for w in warnings))


class TaskRegistryTests(TestCase):
    def test_the_caption_task_is_registered(self):
        task = llm_tasks.get_task("caption_to_template")
        self.assertIsNotNone(task)
        self.assertEqual(task.service_slug, "openrouter_llm")

    def test_an_unknown_task_is_none(self):
        self.assertIsNone(llm_tasks.get_task("nope"))

    def test_the_fingerprint_changes_with_the_prompt_version(self):
        """A rerun after the prompt was edited is a different question."""
        first = llm_tasks.fingerprint("text", ["a"], 1)
        second = llm_tasks.fingerprint("text", ["a"], 2)
        self.assertNotEqual(first, second)

    def test_the_fingerprint_changes_with_the_field_list(self):
        self.assertNotEqual(
            llm_tasks.fingerprint("text", ["a"], 1),
            llm_tasks.fingerprint("text", ["a", "b"], 1),
        )
