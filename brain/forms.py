"""Brain's patient forms: the shared ones, bound to brain's models."""

from common.base_forms import build_patient_forms

from .models import Folder, Patient, Tag

BRAIN_DOMAIN = "brain"

PatientForm, PatientUploadForm, PatientManagementForm = build_patient_forms(
    domain=BRAIN_DOMAIN,
    patient_model=Patient,
    folder_model=Folder,
    tag_model=Tag,
)
