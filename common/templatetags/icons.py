"""`{% icon %}` template tag — renders a Lucide sprite icon.

Usage:
    {% load icons %}
    {% icon "tooth" %}
    {% icon "fa-upload" "ygg-icon-sm" %}
    {% icon project.icon %}          {# resolves a stored "fas fa-brain" #}

Renders an inline SVG referencing the self-hosted sprite:
    <svg class="ygg-icon ..." aria-hidden="true"><use href="/static/icons/lucide-sprite.svg#lc-NAME"></use></svg>
"""

from django import template
from django.templatetags.static import static
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from common.icons import resolve

register = template.Library()


@register.simple_tag
def icon(name, css_class="", *, title=""):
    """Render a Lucide sprite icon by fa/lucide name with optional extra classes."""
    lucide = resolve(name)
    sprite = static("icons/lucide-sprite.svg")
    classes = ("ygg-icon " + css_class).strip()
    title_attr = format_html(' aria-label="{}"', title) if title else mark_safe(' aria-hidden="true"')
    return format_html(
        '<svg class="{}"{}><use href="{}#lc-{}"></use></svg>',
        classes,
        title_attr,
        sprite,
        lucide,
    )
