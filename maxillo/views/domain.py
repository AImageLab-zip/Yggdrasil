"""Route a namespace to its domain models and forms.

The model side moved to ``common/domain_models.py`` with the views that use it
and is re-exported here so existing import paths keep working. The form side
stays: it imports domain forms directly, which ``common/`` may not do.
"""


from django.apps import apps

from common.domain_models import get_domain_models, get_namespace

__all__ = [
    'get_namespace',
    'get_domain_models',
    'get_canonical_models',
    'get_domain_forms',
    'get_canonical_forms',
    'is_laparoscopy_namespace',
]


def is_laparoscopy_namespace(request):
    return get_namespace(request) == 'laparoscopy'


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
