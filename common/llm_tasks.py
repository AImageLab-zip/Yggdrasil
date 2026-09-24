"""What the platform asks a language model to do, declared once per task.

``common/llm.py`` is the transport and ``common/external_config.py`` is the endpoint;
this is the *job*. A task declares which service it uses, which admin-editable prompt it
starts from, how to build the messages, and how to read the answer back. Everything
downstream -- the endpoint, the ``CaptionReport`` row, the admin -- is task-agnostic, so
a second task ("summarise for handover", "translate this report") is a registration here
and needs no new table, endpoint or migration.

Modelled on ``common/export_catalog.py``, same as ``common/external_services.py``.

The first task is ``caption_to_template``: take what a clinician dictated and file it
under the headings of their report template. The instruction is deliberate about what
this is *not* -- it is not summarising, not diagnosing, and not improving the findings.
"""

import hashlib
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

#: The section every task offers for content that fits no template field. Without it a
#: model asked not to drop anything and given nowhere to put something will invent a
#: field or quietly discard the sentence, and discarding is the one thing forbidden.
UNCATEGORISED_KEY = "uncategorised"

#: What that section is called in a finished report. The key is a wire format -- the
#: model writes it and the parser splits on it -- but a clinician reading the report
#: should see a heading, not an identifier, so the rendered text never shows the key.
UNCATEGORISED_LABELS = {
    "it": "Altri rilievi",
    "en": "Other findings",
    "de": "Weitere Befunde",
}

#: The output contract. **Code owns this, not the admin.** The parser depends on it, and
#: an admin edit that broke it would break structuring for every domain at once, with the
#: symptom "my report came back as prose".
OUTPUT_CONTRACT = """
Return the report as plain text using exactly this format, and nothing else:

## <section-key>
<the text for that section>

Rules for the output:
- Use one "## <section-key>" heading per section, spelled exactly as given, in the order given.
- Write the section text as plain prose. No markdown lists, no bold, no nested headings.
- Include a section only if the dictation says something about it. Omit the rest.
- Put anything that was said but fits no section under "## {uncategorised}": stray
  words, test phrases and asides belong there, never in a clinical section.
- A clinical statement always belongs in the section it describes. Do not leave it
  under "## {uncategorised}" because the wording is not an exact match.
- Write no preamble, no closing remarks and no commentary outside the sections.
""".strip()

#: The instruction an admin sees and may rewrite. It is seeded into a ``PromptTemplate``
#: row, which then owns it.
DEFAULT_SYSTEM_PROMPT = """
You are a medical documentation assistant working inside a clinical imaging platform.

A clinician has dictated their findings out loud, and speech-to-text has transcribed
them. Your job is to file what they said under the headings of their reporting
template, written the way a written report is written: the clinician's content, with
the untidiness of speech taken out.

Keep the content exactly:
- Every clinical statement stays. Nothing may be dropped, shortened or summarised.
- Keep hedging and negation exactly as stated: "possible", "probably", "less likely",
  "cannot be excluded", "no", "absent", "not seen". Keep every measurement, every
  laterality and every recommendation.
- Move a statement to the section it belongs in, even if it was dictated out of order,
  and split a sentence that covers two sections.

Remove the untidiness of speech:
- A self-correction replaces what it corrects. "the right kidney, sorry, the left
  kidney" is "the left kidney"; "circa nove millimetri, no, undici" is "11 mm". Write
  only the corrected version: the superseded one must not appear anywhere in the report.
- Leave out fillers, hesitations, false starts and repeated words ("uhm", "er", "eh",
  "okay so", "let me see", "allora", "dunque", "the the"), and spoken dictation
  commands ("comma", "full stop", "new paragraph", "punto e virgola", "a capo",
  "end of dictation").
- Asides that are not about the patient -- a word to a colleague, a phone call, a
  microphone check -- are not report content; see the output format for where they go.

Repair what the transcription got wrong:
- Write spoken numbers as digits, with the unit that was said in its short form and the
  decimal separator of the report's language: "twenty two millimetres" is "22 mm",
  "point three five" is "0.35", "due virgola otto centimetri" is "2,8 cm", "quattro più
  tre" is "4+3", "trenta per cento" is "30%". Never convert one unit into another.
- Restore misheard medical terms when the intended term is unambiguous from context
  ("bi rads four" is "BI-RADS 4", "sub arachnoid" is "subarachnoid", "hydro nephrosis"
  is "hydronephrosis", "a d c" is "ADC"), and write scores and sequences in their
  standard form: PI-RADS, BI-RADS, Gleason, ISUP grade group, T1, T2, FLAIR, DWI, ADC.
- Punctuate and capitalise normally, as complete sentences -- but do not paraphrase.
  Every word that was not an error or a disfluency is the clinician's, and stays theirs.

You must NOT:
- Add any finding, measurement, impression, diagnosis or recommendation that was not
  stated.
- Remove a finding because it seems unimportant, uncertain or repeated.
- Harden a hedge into a finding, or settle a question the clinician left open.
- Answer questions, give advice, or comment on the case.
- Invent a section that was not given to you.

If a passage is unintelligible, keep it verbatim rather than guessing.
""".strip()

DEFAULT_USER_TEMPLATE = """
The clinician dictated in {source_language}. Write every section in {language}.
If those differ, translate faithfully: do not summarise, omit or add while translating.

Report sections, as "key | Label: what belongs in it":
{fields}

The dictated text follows. File all of it.

---
{caption}
---
""".strip()

#: Words too common to prove anything about coverage. Small on purpose: this is a
#: "did a whole clause vanish" check, not a linguistic analysis.
_STOPWORDS = {
    "en": {"the", "a", "an", "and", "or", "of", "in", "on", "is", "are", "was", "were",
           "to", "with", "no", "not", "at", "by", "for", "it", "this", "that", "there"},
    "it": {"il", "lo", "la", "i", "gli", "le", "un", "una", "e", "o", "di", "del", "della",
           "in", "nel", "nella", "con", "non", "per", "che", "si", "al", "alla", "da"},
    "de": {"der", "die", "das", "ein", "eine", "und", "oder", "von", "in", "im", "ist",
           "sind", "war", "mit", "nicht", "kein", "keine", "zu", "auf", "bei", "es"},
    "es": {"el", "la", "los", "las", "un", "una", "y", "o", "de", "del", "en", "con",
           "no", "por", "para", "que", "se", "al"},
    "fr": {"le", "la", "les", "un", "une", "et", "ou", "de", "du", "des", "en", "dans",
           "avec", "ne", "pas", "pour", "que", "se", "au"},
}


def fingerprint(source_text, field_keys, prompt_version):
    """Identifies "the same question asked again" for cache/idempotency purposes."""
    material = "␟".join([
        source_text or "",
        ",".join(field_keys or ()),
        str(prompt_version or 0),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def render_field_list(fields):
    """The template's fields as the model is shown them."""
    lines = []
    for field in fields:
        label = field.get("label") or field.get("key")
        description = (field.get("description") or "").strip()
        line = f"- {field['key']} | {label}"
        if description:
            line += f": {description}"
        lines.append(line)
    lines.append(
        f"- {UNCATEGORISED_KEY} | Other: anything that was said but fits no section above."
    )
    return "\n".join(lines)


SECTION_RE = re.compile(r"^\s{0,3}#{1,6}\s*(?P<key>[A-Za-z0-9_\-]+)\s*$", re.M)


def parse_sections(raw_text, field_keys, labels=None, omit=()):
    """``(structured, rendered, warnings)`` from the model's sectioned plain text.

    ``labels`` maps a section key to the heading a reader should see; anything missing
    falls back to the key read as words. ``structured`` stays keyed by the section key,
    because that is what a rerun, a later edit and the template itself refer to.
    ``omit`` names sections that are parsed and kept but left out of the rendered report.

    Tolerant on purpose. A free-tier model gets the format nearly right nearly always,
    and "nearly" must not lose a clinician's dictation:

    * any heading level is accepted, and a fenced code block around the whole answer is
      stripped;
    * a heading naming a key that was not offered is kept under its own name and reported
      as a warning -- dropping it would discard text, which is the one thing forbidden;
    * text before the first heading is kept under ``uncategorised`` rather than thrown
      away;
    * a model that ignores the format entirely still yields everything it said.
    """
    warnings = []
    text = (raw_text or "").strip()
    if not text:
        return {}, "", [{"code": "empty", "detail": "The model returned nothing."}]

    fenced = re.match(r"^```[a-zA-Z]*\s*\n(?P<body>.*?)\n```$", text, re.S)
    if fenced:
        text = fenced.group("body").strip()

    matches = list(SECTION_RE.finditer(text))
    structured = {}

    if not matches:
        warnings.append({
            "code": "unstructured",
            "detail": "The model did not use the section format; its answer was kept whole.",
        })
        structured[UNCATEGORISED_KEY] = text
    else:
        preamble = text[: matches[0].start()].strip()
        if preamble:
            warnings.append({
                "code": "preamble",
                "detail": "Text before the first section was kept as uncategorised.",
            })
            structured[UNCATEGORISED_KEY] = preamble

        known = set(field_keys) | {UNCATEGORISED_KEY}
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            key = match.group("key").strip()
            body = text[match.end():end].strip()
            if not body:
                continue
            if key not in known:
                warnings.append({
                    "code": "unknown_section",
                    "detail": f"The model invented a section named '{key}'; it was kept.",
                })
            if key in structured:
                structured[key] = f"{structured[key]}\n\n{body}"
            else:
                structured[key] = body

    ordered = [key for key in field_keys if key in structured]
    ordered += [key for key in structured if key not in ordered]
    rendered = render_sections(structured, ordered, labels, omit=omit)
    return structured, rendered, warnings


def humanize_key(key):
    """``subject_specific_findings`` -> ``Subject specific findings``.

    Only ever a fallback: a section the model invented has no label to look up, and its
    key is still better read as words than as an identifier.
    """
    words = str(key or "").replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else ""


def render_sections(structured, ordered, labels=None, omit=()):
    """The report as a clinician reads it: a heading per section, then its text.

    No ``##`` and no section keys. Those belong to the wire format the model writes and
    the parser splits on; carrying them into the report shows the reader a markdown
    artefact and an identifier where their template's own wording should be.

    ``omit`` keeps a section out of the report without discarding it: it stays in
    ``structured``, where a warning and a later reader can still reach it.
    """
    labels = labels or {}
    omit = set(omit or ())
    blocks = []
    for key in ordered:
        if key in omit:
            continue
        heading = labels.get(key) or humanize_key(key)
        blocks.append(f"{heading}\n{structured[key]}" if heading else structured[key])
    return "\n\n".join(blocks)


def _tokens(text, language):
    stop = _STOPWORDS.get(language, _STOPWORDS["en"])
    normalized = unicodedata.normalize("NFKD", text or "").lower()
    words = re.findall(r"[\w']+", normalized, re.UNICODE)
    return {word for word in words if len(word) > 3 and word not in stop}


def coverage_warning(source_text, structured, language, *, threshold=0.75):
    """Flag a report that seems to have lost a chunk of what was dictated.

    An instruction is not an enforcement: "do not drop anything" is a sentence in a
    prompt, and this is the check that it was obeyed. Deliberately a warning and not a
    failure -- a terse but valid caption, or a heavily translated one, can legitimately
    score low, and refusing to show a clinician a usable report because a heuristic was
    unhappy is the worse error. It is also why every template offers an ``uncategorised``
    section.

    Only ever compared within one language: a caption dictated in Italian and reported in
    German shares almost no tokens, so the check is skipped rather than made meaningless.
    """
    if not source_text or not structured:
        return None
    produced = " ".join(str(value) for value in structured.values())
    source_tokens = _tokens(source_text, language)
    if len(source_tokens) < 8:
        return None
    produced_tokens = _tokens(produced, language)
    missing = sorted(source_tokens - produced_tokens)
    kept = 1.0 - (len(missing) / len(source_tokens))
    if kept >= threshold:
        return None
    return {
        "code": "coverage",
        "detail": (
            f"About {round((1 - kept) * 100)}% of what was dictated does not appear in "
            "the structured report. Check nothing was dropped."
        ),
        "missing": missing[:25],
    }


class LlmTask:
    """One thing the platform asks a model to do."""

    def __init__(self, slug, label, *, service_slug, prompt_slug, builder, parser,
                 labeller=None, omit_sections=(), description=""):
        self.slug = slug
        self.label = label
        self.service_slug = service_slug
        self.prompt_slug = prompt_slug
        self.builder = builder
        self.parser = parser
        # Optional: what to call each section in a finished report. Without one the
        # sections are headed by their keys read as words.
        self.labeller = labeller
        # Sections this task parses and stores but keeps out of the report a clinician
        # reads. The endpoint forwards them so the streaming view agrees with the
        # finished report about what is in it.
        self.omit_sections = tuple(omit_sections)
        self.description = description

    def build_messages(self, prompt, context):
        """``(messages, rendered_user_message)`` for ``common.llm``."""
        return self.builder(self, prompt, context)

    def parse(self, raw_text, context):
        return self.parser(raw_text, context)

    def section_labels(self, context):
        """``{section key: heading}``, for a caller that renders before the parse.

        The streaming endpoint needs this: it is forwarding the model's own text, which
        is keyed, while the reader wants headings.
        """
        return self.labeller(context) if self.labeller else {}

    def __str__(self):  # pragma: no cover - admin/debug convenience
        return self.label


def _caption_builder(task, prompt, context):
    fields = context.get("fields") or []
    values = {
        "language": context.get("report_language") or "it",
        "source_language": context.get("source_language") or "the same language",
        "fields": render_field_list(fields),
        "caption": context.get("caption") or "",
    }
    user_message = prompt.render_user_message(values)
    system = prompt.system_prompt.strip() + "\n\n" + OUTPUT_CONTRACT.format(
        uncategorised=UNCATEGORISED_KEY
    )
    return (
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ],
        user_message,
    )


def _caption_parser(raw_text, context):
    fields = context.get("fields") or []
    field_keys = [field["key"] for field in fields]
    structured, rendered, warnings = parse_sections(
        raw_text,
        field_keys,
        caption_section_labels(context),
        # The report is the template's own headings and nothing else. Whatever fitted
        # none of them is still parsed and stored -- it is the clinician's dictation --
        # but a report is a clinical document, and a trailing "other findings" heading
        # holding stray words is not part of one.
        omit=(UNCATEGORISED_KEY,),
    )

    leftover = (structured.get(UNCATEGORISED_KEY) or "").strip()
    if leftover:
        # The text itself is kept in ``structured`` and not repeated here: a warning
        # that quotes the dictation puts clinical wording back on screen next to a
        # report that deliberately leaves it out.
        filed_anything = any(
            key != UNCATEGORISED_KEY and (value or "").strip()
            for key, value in structured.items()
        )
        if filed_anything:
            warnings.append({
                "code": "not_filed",
                "detail": (
                    "Part of the dictation fitted no field of this template and is not "
                    "in the report."
                ),
            })
        else:
            # An empty report with a mild note under it reads like a failure nobody
            # explained. The likeliest cause is a real one worth naming: the dictation
            # and the template are about different things.
            warnings.append({
                "code": "nothing_filed",
                "detail": (
                    "None of this dictation fitted the fields of this template, so the "
                    "report is empty. Check the caption's modality, or ask an "
                    "administrator for a template that matches this kind of dictation."
                ),
            })

    same_language = (
        context.get("source_language")
        and context.get("source_language") == context.get("report_language")
    )
    if same_language:
        # Coverage is judged on what the report actually says, so text that only ever
        # reached the omitted section counts as missing rather than as covered.
        reported = {
            key: value for key, value in structured.items() if key != UNCATEGORISED_KEY
        }
        coverage = coverage_warning(
            context.get("caption"), reported, context.get("report_language")
        )
        if coverage:
            warnings.append(coverage)
    return structured, rendered, warnings


def caption_section_labels(context):
    """The heading for every section this task can produce, in the report's language.

    The template's own wording for its fields -- which the clinician recognises, because
    it is what they were reading while dictating -- plus a name for the catch-all.
    """
    labels = {
        field["key"]: field.get("label") or humanize_key(field["key"])
        for field in (context.get("fields") or [])
    }
    language = context.get("report_language") or "it"
    labels[UNCATEGORISED_KEY] = UNCATEGORISED_LABELS.get(
        language, UNCATEGORISED_LABELS["en"]
    )
    return labels


CAPTION_TO_TEMPLATE = LlmTask(
    "caption_to_template",
    "Dictated caption into the report template",
    service_slug="openrouter_llm",
    prompt_slug="caption_to_template",
    builder=_caption_builder,
    parser=_caption_parser,
    labeller=caption_section_labels,
    omit_sections=(UNCATEGORISED_KEY,),
    description=(
        "Files a dictated caption under the headings of the patient's report template, "
        "repairing transcription errors without changing what was said."
    ),
)

LLM_TASKS = {CAPTION_TO_TEMPLATE.slug: CAPTION_TO_TEMPLATE}


def get_task(slug):
    return LLM_TASKS.get(str(slug or "").strip())
