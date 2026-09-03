from django import forms

from common.models import Project, ProjectAccess
from common.permissions import filter_folders_for_user
from .models import Dataset, Folder, Patient, Tag

BRAIN_DOMAIN = "brain"


class PatientForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = []


class PatientUploadForm(forms.ModelForm):
    project = forms.ModelChoiceField(
        queryset=Project.objects.none(),
        required=True,
        label='Project',
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    folder = forms.ModelChoiceField(
        queryset=Folder.objects.none(),
        required=True,
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    tags_text = forms.CharField(
        required=False,
        help_text='Comma-separated tags',
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. caseA, urgent'}),
    )

    class Meta:
        model = Patient
        fields = ['name', 'project', 'folder']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Patient X'}),
        }
        labels = {
            'name': 'Scan Name',
            'folder': 'Folder',
        }

    def __init__(self, *args, user=None, current_project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['folder'].required = True
        if user:
            projects_qs = Project.objects.filter(domain=BRAIN_DOMAIN, is_active=True)
            if not user.is_staff:
                accessible = ProjectAccess.objects.filter(user=user).values_list('project_id', flat=True)
                projects_qs = projects_qs.filter(id__in=accessible)
            self.fields['project'].queryset = projects_qs.order_by('name')

            project_id = None
            if self.data:
                project_id = self.data.get('project') or self.data.get('project_id')
            if not project_id and current_project:
                project_id = getattr(current_project, 'id', current_project)
            if project_id:
                self.fields['folder'].queryset = (
                    Folder.objects.filter(project_id=project_id, parent__isnull=True).order_by('name')
                )
                self.fields['project'].initial = project_id
        else:
            self.fields['project'].queryset = Project.objects.none()
            self.fields['folder'].queryset = Folder.objects.none()

    def clean(self):
        cleaned_data = super().clean()
        project = cleaned_data.get('project')
        folder = cleaned_data.get('folder')
        if project and folder and folder.project_id != project.id:
            raise forms.ValidationError(
                'The selected folder does not belong to the selected project.'
            )
        if not folder:
            raise forms.ValidationError('A folder is required.')
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit)
        project = self.cleaned_data.get('project')
        if project:
            instance.project = project
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
        scope_project_id = getattr(self.instance, 'project_id', None)
        if user:
            folders_qs = Folder.objects.filter(parent__isnull=True).order_by('name')
            if scope_project_id:
                folders_qs = folders_qs.filter(project_id=scope_project_id)
            self.fields['folder'].queryset = filter_folders_for_user(user, folders_qs, BRAIN_DOMAIN)
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
