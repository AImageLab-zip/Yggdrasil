"""Helpers to route maxillo/laparoscopy namespaces to the correct domain models/forms."""


from django.apps import apps


def get_namespace(request):
    return (getattr(request, 'resolver_match', None) and request.resolver_match.namespace) or 'maxillo'


def is_laparoscopy_namespace(request):
    return get_namespace(request) == 'laparoscopy'


def get_domain_models(request):

    ns = get_namespace(request)
    if ns == 'laparoscopy':
        app_label = 'laparoscopy'
    else:
        app_label = 'maxillo'

    models = {
        'Patient': apps.get_model(app_label, 'Patient'),
        'Folder': apps.get_model(app_label, 'Folder'),
        'Tag': apps.get_model(app_label, 'Tag'),
        'Dataset': apps.get_model(app_label, 'Dataset'),
        'Classification': apps.get_model(app_label, 'Classification'),
        'VoiceCaption': apps.get_model(app_label, 'VoiceCaption'),
        'Export': apps.get_model(app_label, 'Export'),
    }
    return models


def get_canonical_models():
    """Return maxillo models used as canonical write target in transition phase."""
    return {
        'Patient': apps.get_model('maxillo', 'Patient'),
        'Folder': apps.get_model('maxillo', 'Folder'),
        'Tag': apps.get_model('maxillo', 'Tag'),
        'Dataset': apps.get_model('maxillo', 'Dataset'),
        'Classification': apps.get_model('maxillo', 'Classification'),
        'VoiceCaption': apps.get_model('maxillo', 'VoiceCaption'),
        'Export': apps.get_model('maxillo', 'Export'),
    }


def get_domain_forms(request):
    ns = get_namespace(request)
    if ns == 'laparoscopy':
        from laparoscopy.forms import (
            ClassificationForm,
            DatasetForm,
            PatientForm,
            PatientManagementForm,
            PatientUploadForm,
        )
    else:
        from ..forms import (
            ClassificationForm,
            DatasetForm,
            PatientForm,
            PatientManagementForm,
            PatientUploadForm,
        )

    return {
        'PatientForm': PatientForm,
        'PatientUploadForm': PatientUploadForm,
        'PatientManagementForm': PatientManagementForm,
        'ClassificationForm': ClassificationForm,
        'DatasetForm': DatasetForm,
    }


def get_canonical_forms():
    """Return maxillo forms used as canonical write forms in transition phase."""
    from ..forms import (
        ClassificationForm,
        DatasetForm,
        PatientForm,
        PatientManagementForm,
        PatientUploadForm,
    )

    return {
        'PatientForm': PatientForm,
        'PatientUploadForm': PatientUploadForm,
        'PatientManagementForm': PatientManagementForm,
        'ClassificationForm': ClassificationForm,
        'DatasetForm': DatasetForm,
    }
