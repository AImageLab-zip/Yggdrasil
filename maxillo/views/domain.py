"""Helpers to route maxillo/laparoscopy namespaces to the correct domain models/forms."""


from django.apps import apps


def get_namespace(request):
    return (getattr(request, 'resolver_match', None) and request.resolver_match.namespace) or 'maxillo'


def is_laparoscopy_namespace(request):
    return get_namespace(request) == 'laparoscopy'


#: Models every domain declares, and those only some do. A domain app is not
#: obliged to have a Classification -- brain and urology have none -- so asking
#: for one must come back empty rather than raise or fall back to maxillo's.
_REQUIRED_MODELS = ('Patient', 'Folder', 'Tag', 'VoiceCaption', 'Export')
_OPTIONAL_MODELS = ('Classification',)


def get_domain_models(request):
    """Model classes for the namespace being browsed, from the domain registry.

    Resolved through ``common.domains.normalize_domain`` rather than a branch per
    domain, so a newly registered domain is served without editing this function.
    Optional models are absent from the returned dict; use ``.get()`` for those.
    """
    from common.domains import normalize_domain

    app_label = normalize_domain(get_namespace(request))

    models = {name: apps.get_model(app_label, name) for name in _REQUIRED_MODELS}
    for name in _OPTIONAL_MODELS:
        try:
            models[name] = apps.get_model(app_label, name)
        except LookupError:
            pass
    return models


def get_canonical_models():
    """Return maxillo models used as canonical write target in transition phase."""
    return {
        'Patient': apps.get_model('maxillo', 'Patient'),
        'Folder': apps.get_model('maxillo', 'Folder'),
        'Tag': apps.get_model('maxillo', 'Tag'),
        'Classification': apps.get_model('maxillo', 'Classification'),
        'VoiceCaption': apps.get_model('maxillo', 'VoiceCaption'),
        'Export': apps.get_model('maxillo', 'Export'),
    }


def get_domain_forms(request):
    ns = get_namespace(request)
    if ns == 'laparoscopy':
        from laparoscopy.forms import (
            ClassificationForm,
            PatientForm,
            PatientManagementForm,
            PatientUploadForm,
        )
    else:
        from ..forms import (
            ClassificationForm,
            PatientForm,
            PatientManagementForm,
            PatientUploadForm,
        )

    return {
        'PatientForm': PatientForm,
        'PatientUploadForm': PatientUploadForm,
        'PatientManagementForm': PatientManagementForm,
        'ClassificationForm': ClassificationForm,
    }


def get_canonical_forms():
    """Return maxillo forms used as canonical write forms in transition phase."""
    from ..forms import (
        ClassificationForm,
        PatientForm,
        PatientManagementForm,
        PatientUploadForm,
    )

    return {
        'PatientForm': PatientForm,
        'PatientUploadForm': PatientUploadForm,
        'PatientManagementForm': PatientManagementForm,
        'ClassificationForm': ClassificationForm,
    }
