"""The reporting-checklist panel, rendered from the database.

An inclusion tag rather than a context processor: a context processor runs on every
request in the application and would add two queries to every page for a panel that
appears on one. The tag takes the namespace and the session's project from the context it
is rendered in, exactly as the hardcoded panel used to read ``ns`` and ``report_language``.
"""

from django import template

from common.report_templates import (
    DEFAULT_REPORT_LANGUAGE,
    REPORT_LANGUAGES,
    modality_slugs_with_templates,
    normalize_language,
    panel_groups_by_language,
)

register = template.Library()


@register.inclusion_tag(
    "common/sections/report_template_section.html", takes_context=True
)
def report_template_panel(context):
    """Resolve the panel for whatever page this is rendered on.

    Returns ``groups == []`` for a domain with no template, and the panel then renders
    nothing at all -- which is what replaced the ``{% if ns != 'laparoscopy' %}`` guard
    around the include.
    """
    request = context.get("request")
    namespace = ""
    if request is not None and getattr(request, "resolver_match", None) is not None:
        namespace = request.resolver_match.namespace or ""
    namespace = namespace or "maxillo"

    project = context.get("current_project")
    language = normalize_language(context.get("report_language") or DEFAULT_REPORT_LANGUAGE)

    by_language = panel_groups_by_language(namespace, project)
    return {
        # Emptiness is decided once, here, rather than by each of the three
        # per-language blocks discovering it separately.
        "has_template": any(groups for _language, groups in by_language),
        "groups_by_language": by_language,
        "report_language": language,
        "report_languages": REPORT_LANGUAGES,
        "template_modalities": modality_slugs_with_templates(namespace, project),
    }
