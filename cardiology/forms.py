"""Cardiology's forms.

The shared patient forms (``common/base_forms.py``) with an ECG input on the
upload form -- validated there, so a bad file never creates a patient -- and the
role-dependent ``visibility`` field on the management form.
"""

from django import forms

from common.base_forms import build_patient_forms

from .file_utils import InvalidEcgFile, validate_ecg_json
from .models import Folder, Patient, Tag

CARDIOLOGY_DOMAIN = "cardiology"

PatientForm, _SharedUploadForm, _SharedManagementForm = build_patient_forms(
    domain=CARDIOLOGY_DOMAIN,
    patient_model=Patient,
    folder_model=Folder,
    tag_model=Tag,
    name_label="Patient Name",
    # Field name matches the 'ecg' Modality slug: templates/common/upload/modalities/ecg.html
    # renders it as `patient_upload_form.ecg`, the convention laparoscopy's `video` uses.
    upload_extra_fields={
        "ecg": forms.FileField(
            required=False,
            label="ECG recording (JSON)",
            widget=forms.FileInput(attrs={"class": "form-control", "accept": ".json"}),
        ),
    },
)


class PatientUploadForm(_SharedUploadForm):
    def clean_ecg(self):
        ecg = self.cleaned_data.get("ecg")
        if not ecg:
            raise forms.ValidationError("Add an ECG JSON file before uploading.")
        try:
            validate_ecg_json(ecg)
        except InvalidEcgFile as exc:
            raise forms.ValidationError(str(exc)) from exc
        return ecg


class PatientManagementForm(_SharedManagementForm):
    class Meta(_SharedManagementForm.Meta):
        fields = ["name", "visibility", "folder"]
        widgets = {
            **_SharedManagementForm.Meta.widgets,
            "visibility": forms.Select(attrs={"class": "form-select form-select-sm"}),
        }
        labels = {**_SharedManagementForm.Meta.labels, "visibility": "Visibility"}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, user=user, **kwargs)
        profile = getattr(user, "profile", None) if user else None
        if profile:
            if profile.is_student_developer():
                self.fields["visibility"].choices = [("debug", "Debug")]
            elif profile.is_admin():
                self.fields["visibility"].choices = Patient.VISIBILITY_CHOICES
            else:
                self.fields["visibility"].choices = [("public", "Public"), ("private", "Private")]
