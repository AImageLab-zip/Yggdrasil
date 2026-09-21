"""Resolve a request's domain models from the registry.

Views that are the same in every domain -- folders, tags, voice captions,
exports -- need that domain's model classes and nothing else about it. They get
them here, keyed off the URL namespace and resolved through
``common.domains``, so adding a domain needs no edit to any of them.

This lives in ``common/`` rather than a domain app because those views do too.
It resolves models with ``apps.get_model`` and never imports a domain app, which
is what lets it.
"""

from django.apps import apps

#: Models every domain declares, and those only some do. A domain app is not
#: obliged to have a Classification -- brain and urology have none -- so asking
#: for one comes back empty rather than raising or borrowing maxillo's.
REQUIRED_MODELS = ("Patient", "Folder", "Tag", "VoiceCaption", "Export")
OPTIONAL_MODELS = ("Classification",)


def get_namespace(request):
    """The URL namespace being served, defaulting to the platform's first domain."""
    from common.domains import DEFAULT_DOMAIN

    match = getattr(request, "resolver_match", None)
    return (match and match.namespace) or DEFAULT_DOMAIN


def get_domain_models(request):
    """Model classes for the namespace being browsed.

    Optional models are absent from the returned dict; use ``.get()`` for those.
    """
    from common.domains import normalize_domain

    app_label = normalize_domain(get_namespace(request))

    models = {name: apps.get_model(app_label, name) for name in REQUIRED_MODELS}
    for name in OPTIONAL_MODELS:
        try:
            models[name] = apps.get_model(app_label, name)
        except LookupError:
            pass
    return models
