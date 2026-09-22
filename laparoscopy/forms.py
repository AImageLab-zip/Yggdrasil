"""Laparoscopy's forms.

The patient and upload forms are the shared ones with a video input added; the
management form is not -- it carries a role-dependent `visibility` field that no
other domain has -- and ClassificationForm is laparoscopy's own.
"""

from django import forms

from common.base_forms import build_patient_forms
from common.permissions import filter_folders_for_user  # noqa: F401  (used below)
from common.models import Project, ProjectAccess  # noqa: F401  (used below)
from .models import Classification, Folder, Patient, Tag

LAPAROSCOPY_DOMAIN = "laparoscopy"

PatientForm, PatientUploadForm, _ = build_patient_forms(
    domain=LAPAROSCOPY_DOMAIN,
    patient_model=Patient,
    folder_model=Folder,
    tag_model=Tag,
    name_label="Patient Name",
    upload_extra_fields={
        "video": forms.FileField(
            required=False,
            label="Video",
            widget=forms.FileInput(
                attrs={"class": "form-control", "accept": ".mp4,.avi"}
            ),
        ),
    },
)


class ClassificationForm(forms.ModelForm):
    class Meta:
        model = Classification
        fields = ['notes']
        widgets = {
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }


class PatientManagementForm(forms.ModelForm):
    folder = forms.ModelChoiceField(
        queryset=Folder.objects.all().order_by('name'),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select form-select-sm'}),
    )
    tags_text = forms.CharField(
        required=False,
        help_text='Comma-separated tags',
        widget=forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'e.g. caseA, urgent'}),
    )

    class Meta:
        model = Patient
        fields = ['name', 'visibility', 'folder']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Patient name'}),
            'visibility': forms.Select(attrs={'class': 'form-select form-select-sm'}),
        }
        labels = {
            'name': 'Name',
            'visibility': 'Visibility',
            'folder': 'Folder',
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['tags_text'].initial = ', '.join(self.instance.tag_names())

        if user and hasattr(user, 'profile'):
            if user.profile.is_student_developer():
                self.fields['visibility'].choices = [('debug', 'Debug')]
            elif user.profile.is_admin():
                self.fields['visibility'].choices = Patient.VISIBILITY_CHOICES
            else:
                self.fields['visibility'].choices = [
                    ('public', 'Public'),
                    ('private', 'Private'),
                ]

    def clean(self):
        cleaned_data = super().clean()
        name = cleaned_data.get('name')
        if name and len(name.strip()) == 0:
            raise forms.ValidationError('Patient name cannot be empty.')
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit)
        tags_text = self.cleaned_data.get('tags_text', '') or ''
        tag_names = [tag.strip() for tag in tags_text.split(',') if tag.strip()]
        if commit:
            tags = []
            for name in tag_names:
                tag, _ = Tag.objects.get_or_create(name=name)
                tags.append(tag)
            instance.tags.set(tags)
        return instance
