"""Which report template applies here, and what it looks like once resolved.

The accessor module for ``ReportTemplate`` -- the same shape as
``common/modality_config.py`` and ``common/external_config.py``, and for the same reason:
one place decides, everywhere else asks.

What this replaces is a 367-line ``{% if ns == 'brain' %}/{% elif ns == 'urology' %}``
ladder in a template. There is no domain branch here and there must not be one: urology
having three checklists and brain one is **data** -- a different number of rows -- not a
different code path. If a future domain needs something this cannot express, the answer
is another column, not an ``if``.
"""
import logging

from django.db.utils import DatabaseError

logger = logging.getLogger(__name__)

#: The languages the panel and the structured report are written in. Kept beside
#: ``UserPreference.LANGUAGE_CHOICES``; widening it means new columns on
#: ``ReportTemplateField``, which is a migration and deliberately visible.
REPORT_LANGUAGES = ("en", "it", "de")
DEFAULT_REPORT_LANGUAGE = "it"


def normalize_language(language):
    language = str(language or "").strip().lower()
    return language if language in REPORT_LANGUAGES else DEFAULT_REPORT_LANGUAGE


def _project_id(project):
    return getattr(project, "pk", None) if project is not None else None


def normalize_modality(domain, modality_slug):
    """The bare modality a template is filed under.

    Urology's modality slugs are ``urology-mri`` / ``urology-wsi`` / ``urology-confocal``,
    but the panel files its checklists under ``mri`` / ``wsi`` / ``confocal`` -- the
    urology page strips the prefix in JS (``patient_detail_content.html``), and the
    hardcoded HTML was written to match. A caption carries the *full* slug, so a lookup
    from the structuring path would otherwise silently find no template.

    This is a naming convention, not a domain branch: any domain whose modality slugs are
    prefixed with its own name resolves the same way, and one whose slugs are bare is
    unaffected because the prefix is simply absent.
    """
    modality_slug = str(modality_slug or "").strip()
    domain = str(domain or "").strip()
    prefix = f"{domain}-"
    if domain and modality_slug.startswith(prefix):
        return modality_slug[len(prefix):]
    return modality_slug


def template_for(domain, modality_slug="", project=None):
    """The template that applies, or ``None``.

    Narrowing order, first hit wins: this project's template for this modality, the
    domain's template for this modality, this project's catch-all, the domain's catch-all.

    The final ``order_by`` is not redundant with the uniqueness constraint. A constraint
    can be added after rows exist, can be dropped by a migration, and cannot be trusted to
    have held on every database this code runs against -- and a lookup that raises
    ``MultipleObjectsReturned`` in front of a clinician is a worse failure than picking
    the most recently edited one.
    """
    from common.models import ReportTemplate

    domain = str(domain or "").strip()
    if not domain:
        return None
    modality_slug = normalize_modality(domain, modality_slug)
    project_id = _project_id(project)

    scopes = []
    if modality_slug and project_id:
        scopes.append({"modality_slug": modality_slug, "project_id": project_id})
    if modality_slug:
        scopes.append({"modality_slug": modality_slug, "project_id": None})
    if project_id:
        scopes.append({"modality_slug": "", "project_id": project_id})
    scopes.append({"modality_slug": "", "project_id": None})

    try:
        for scope in scopes:
            found = (
                ReportTemplate.objects.filter(domain=domain, is_active=True, **scope)
                .order_by("-updated_at", "-id")
                .first()
            )
            if found is not None:
                return found
    except DatabaseError:
        logger.warning("ReportTemplate lookup failed for '%s'; rendering no panel", domain)
        return None
    return None


def templates_for(domain, project=None):
    """Every template that should appear in the panel for this domain, in order.

    A domain with per-modality templates (urology) yields one group per modality; a domain
    with a single checklist yields one; a domain with none yields an empty list, and the
    panel then renders nothing at all. That empty case is laparoscopy, and it is why the
    panel needs no ``{% if ns != 'laparoscopy' %}`` guard any more.
    """
    from common.models import ReportTemplate

    domain = str(domain or "").strip()
    if not domain:
        return []
    project_id = _project_id(project)

    try:
        candidates = list(
            ReportTemplate.objects.filter(domain=domain, is_active=True)
            .prefetch_related("fields")
            .order_by("modality_slug", "name")
        )
    except DatabaseError:
        logger.warning("ReportTemplate lookup failed for '%s'; rendering no panel", domain)
        return []

    # A project's own template shadows the domain default for the same modality, rather
    # than appearing beside it.
    chosen = {}
    for template in candidates:
        if template.project_id not in (None, project_id):
            continue
        key = template.modality_slug
        current = chosen.get(key)
        if current is None or (current.project_id is None and template.project_id is not None):
            chosen[key] = template

    # An explicit per-modality template replaces the catch-all for the modalities it
    # covers, so showing both would list the same checklist twice.
    if len(chosen) > 1:
        chosen.pop("", None)
    return [chosen[key] for key in sorted(chosen)]


def field_records(template, language):
    """``[{key, label, description}]`` for one template, resolved into ``language``."""
    language = normalize_language(language)
    return [
        {
            "key": field.key,
            "label": field.label_for(language),
            "description": field.description_for(language),
            "is_required": field.is_required,
        }
        for field in template.fields.all()
    ]


def panel_groups(domain, project=None, language=DEFAULT_REPORT_LANGUAGE):
    """What the reporting panel renders, already resolved into one language.

    Returns ``[{modality_slug, title, icon, fields: [...]}]``. The template loops it; it
    makes no decisions of its own, and it never learns which domain it is rendering.
    """
    language = normalize_language(language)
    groups = []
    for template in templates_for(domain, project):
        groups.append({
            "slug": template.slug,
            "modality_slug": template.modality_slug,
            "title": template.subtitle_for(language),
            "icon": template.icon,
            "fields": field_records(template, language),
        })
    return groups


def panel_groups_by_language(domain, project=None):
    """``[(language, groups), ...]`` for every reporting language.

    The panel renders all three and hides two with ``d-none``, which is what it did when
    the content was hardcoded. Keeping that shape means the existing language toggle --
    plain class switching, no fetch, no reload -- keeps working untouched, and there is
    no moment mid-dictation where the checklist is blank while a request is in flight.
    The cost is three copies of a short list in the DOM, which is what was there before.
    """
    resolved = templates_for(domain, project)
    return [
        (
            language,
            [
                {
                    "slug": template.slug,
                    "modality_slug": template.modality_slug,
                    "title": template.subtitle_for(language),
                    "icon": template.icon,
                    "fields": field_records(template, language),
                }
                for template in resolved
            ],
        )
        for language in REPORT_LANGUAGES
    ]


def modality_slugs_with_templates(domain, project=None):
    """Which modality slugs have a checklist -- ``["*"]`` when one covers them all.

    The browser needs this to decide whether the structuring button applies to the
    modality currently on screen, which in urology changes without a page load.
    """
    slugs = [t.modality_slug for t in templates_for(domain, project)]
    if any(slug == "" for slug in slugs):
        return ["*"]
    return slugs
