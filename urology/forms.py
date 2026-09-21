"""Urology's patient forms: the shared ones, bound to urology's models."""

from common.base_forms import build_patient_forms

from .models import Folder, Patient, Tag

UROLOGY_DOMAIN = "urology"

PatientForm, PatientUploadForm, PatientManagementForm = build_patient_forms(
    domain=UROLOGY_DOMAIN,
    patient_model=Patient,
    folder_model=Folder,
    tag_model=Tag,
    tags_placeholder="e.g. biopsy, Gleason7",
)
