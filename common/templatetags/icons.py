"""`{% icon %}` template tag — renders a Font Awesome 6 glyph.

Usage:
    {% load icons %}
    {% icon "tooth" %}                    -> <i class="fas fa-tooth" aria-hidden="true"></i>
    {% icon "fa-upload" "ygg-icon-sm" %}  -> adds extra classes
    {% icon modality.icon %}              -> resolves a stored "fas fa-brain"
    {% icon "trash" title="Delete" %}     -> labelled instead of aria-hidden

Font Awesome is self-hosted (static/vendor/fontawesome) and loaded sitewide in
base.html. Name normalisation/aliasing lives in common/icons.py.
"""

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from common.icons import resolve

register = template.Library()


@register.simple_tag
def icon(name, css_class="", *, title=""):
    """Render a Font Awesome icon by name with optional extra classes."""
    fa_classes = resolve(name)
    if not fa_classes:
        return ""
    classes = (fa_classes + " " + css_class).strip() if css_class else fa_classes
    title_attr = format_html(' aria-label="{}"', title) if title else mark_safe(' aria-hidden="true"')
    return format_html('<i class="{}"{}></i>', classes, title_attr)
