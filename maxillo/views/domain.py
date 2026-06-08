"""Helpers for Maxillo domain models/forms."""

from django.apps import apps


def get_namespace(request):
    return 'maxillo'


def get_domain_models(request):
    return {
        'Patient': apps.get_model('maxillo', 'Patient'),
        'Folder': apps.get_model('maxillo', 'Folder'),
        'Tag': apps.get_model('maxillo', 'Tag'),
        'Dataset': apps.get_model('maxillo', 'Dataset'),
        'Classification': apps.get_model('maxillo', 'Classification'),
        'VoiceCaption': apps.get_model('maxillo', 'VoiceCaption'),
        'Export': apps.get_model('maxillo', 'Export'),
    }


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
