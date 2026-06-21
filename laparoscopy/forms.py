from django import forms

from .models import Classification, Dataset, Folder, Patient, Tag


class PatientForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = []


class PatientUploadForm(forms.ModelForm):
    video = forms.FileField(
        required=False,
        label='Video',
        widget=forms.FileInput(
            attrs={
                'class': 'form-control',
                'accept': '.mp4,.avi',
            }
        ),
    )

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
        fields = ['name', 'visibility', 'folder']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Patient X'}),
            'visibility': forms.Select(attrs={'class': 'form-control'}),
        }
        labels = {
            'name': 'Patient Name',
            'visibility': 'Visibility',
            'folder': 'Folder',
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user and hasattr(user, 'profile'):
            if user.profile.is_student_developer():
                self.fields['visibility'].choices = [('debug', 'Debug')]
                self.fields['visibility'].initial = 'debug'
                self.fields['visibility'].widget.attrs['readonly'] = True
            elif user.profile.is_admin():
                self.fields['visibility'].choices = Patient.VISIBILITY_CHOICES
            else:
                self.fields['visibility'].choices = [
                    ('public', 'Public'),
                    ('private', 'Private'),
                ]

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
        fields = ['name', 'visibility', 'dataset', 'folder']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Patient name'}),
            'visibility': forms.Select(attrs={'class': 'form-select form-select-sm'}),
            'dataset': forms.Select(attrs={'class': 'form-select form-select-sm'}),
        }
        labels = {
            'name': 'Name',
            'visibility': 'Visibility',
            'dataset': 'Dataset',
            'folder': 'Folder',
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['dataset'].empty_label = 'No Dataset'
        self.fields['dataset'].required = False
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


class DatasetForm(forms.ModelForm):
    class Meta:
        model = Dataset
        fields = ['name', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': 'Dataset name'}),
            'description': forms.Textarea(attrs={'class': 'form-control form-control-sm', 'rows': 2, 'placeholder': 'Optional description'}),
        }
