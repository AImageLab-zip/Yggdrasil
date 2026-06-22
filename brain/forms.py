from django import forms

from common.permissions import filter_folders_for_user
from .models import Dataset, Folder, Patient, Tag


class PatientForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = []


class PatientUploadForm(forms.ModelForm):
    folder = forms.ModelChoiceField(
        queryset=Folder.objects.all().order_by('name'),
        required=False,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    tags_text = forms.CharField(
        required=False,
        help_text='Comma-separated tags',
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. caseA, urgent'}),
    )

    class Meta:
        model = Patient
        fields = ['name', 'folder']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Patient X'}),
        }
        labels = {
            'name': 'Scan Name',
            'folder': 'Folder',
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user:
            folders_qs = Folder.objects.filter(parent__isnull=True).order_by('name')
            self.fields['folder'].queryset = filter_folders_for_user(user, folders_qs, 'brain')
        else:
            self.fields['folder'].queryset = Folder.objects.none()

    def save(self, commit=True):
        instance = super().save(commit)
        tags_text = self.cleaned_data.get('tags_text', '') or ''
        tag_names = [tag.strip() for tag in tags_text.split(',') if tag.strip()]
        if commit and tag_names:
            tags = []
            for name in tag_names:
                tag, _ = Tag.objects.get_or_create(name=name)
                tags.append(tag)
            instance.tags.set(tags + list(instance.tags.all()))
        return instance


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
        fields = ['name', 'dataset', 'folder']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Scan name'}),
            'dataset': forms.Select(attrs={'class': 'form-select form-select-sm'}),
        }
        labels = {
            'name': 'Name',
            'dataset': 'Dataset',
            'folder': 'Folder',
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['dataset'].empty_label = 'No Dataset'
        self.fields['dataset'].required = False
        if user:
            folders_qs = Folder.objects.filter(parent__isnull=True).order_by('name')
            self.fields['folder'].queryset = filter_folders_for_user(user, folders_qs, 'brain')
        else:
            self.fields['folder'].queryset = Folder.objects.none()
        if self.instance and self.instance.pk:
            self.fields['tags_text'].initial = ', '.join(self.instance.tag_names())

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


class DatasetForm(forms.ModelForm):
    class Meta:
        model = Dataset
        fields = ['name', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Dataset name'}),
            'description': forms.Textarea(attrs={'class': 'form-control form-control-sm', 'rows': 2, 'placeholder': 'Optional description'}),
        }
