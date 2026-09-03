"""``{% cornerstone_entry %}`` -- load one per-surface Cornerstone3D bundle.

Usage::

    {% load cornerstone %}
    {% cornerstone_entry 'volume-grid' %}

Mirrors ``common/templatetags/icons.py`` + ``common/icons.py``: the tag is thin, and
resolution lives in ``common/cornerstone_assets.py``.

The ``type="module"`` is not cosmetic. Finding F4 of docs/cornerstone-roadmap.md:
``import.meta`` is unavailable in esbuild's IIFE output, and three vendored packages
resolve their web workers via ``new URL(..., import.meta.url)``, so an IIFE bundle
loses every worker. ESM output plus a module script tag is the only combination that
works, which is why this tag has no non-module variant.

Nothing calls this yet -- Phase 1 builds and verifies the bundle, and Phases 3-10 wire
the surfaces one at a time.
"""

from django import template
from django.templatetags.static import static
from django.utils.html import format_html

from common.cornerstone_assets import entry_static_path

register = template.Library()


@register.simple_tag
def cornerstone_entry(name):
    """Render the module script tag for one surface, or a console error.

    A missing, malformed or incomplete bundle must never be a 500 -- the page still
    has to render so the rest of the patient record stays reachable. It degrades to a
    console error instead, which is loud in development and harmless in production.
    """
    path = entry_static_path(name)
    if path is None:
        return format_html(
            "<script>console.error("
            "'Cornerstone bundle entry \"{}\" is unavailable: '"
            " + 'static/vendor/cornerstone/ has no usable manifest.json for it. '"
            " + 'Run scripts/build_frontend.sh.');</script>",
            name,
        )
    return format_html('<script type="module" src="{}"></script>', static(path))
