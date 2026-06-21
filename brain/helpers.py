"""Brain view helper utilities."""

from django.shortcuts import redirect, render
from django.template.loader import select_template
from django.urls import NoReverseMatch


def render_with_fallback(request, base_template_name: str, context: dict):
    candidates = [
        f"brain/{base_template_name}.html",
        f"common/{base_template_name}.html",
    ]
    template = select_template(candidates)
    return render(request, template.template.name, context)


def redirect_with_namespace(request, name: str, *args, **kwargs):
    try:
        return redirect(f"brain:{name}", *args, **kwargs)
    except NoReverseMatch:
        try:
            return redirect(name, *args, **kwargs)
        except NoReverseMatch:
            return redirect("/")
