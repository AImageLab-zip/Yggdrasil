from django import forms

from .models import Classification, Dataset, Folder, Patient, Tag


class PatientForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = []


class PatientUploadForm(forms.ModelForm):
    ios_upper = forms.FileField(
        required=False,
        label='IOS - Upper',
        widget=forms.FileInput(attrs={'class': 'form-control', 'accept': '.stl'}),
    )
    ios_lower = forms.FileField(
        required=False,
        label='IOS - Lower',
        widget=forms.FileInput(attrs={'class': 'form-control', 'accept': '.stl'}),
    )

    teleradiography = forms.FileField(
        required=False,
        label='Teleradiography',
        widget=forms.FileInput(attrs={'class': 'form-control', 'accept': '.jpg,.jpeg,.png'}),
    )

    panoramic = forms.FileField(
        required=False,
        label='Panoramic',
        widget=forms.FileInput(attrs={'class': 'form-control', 'accept': '.jpg,.jpeg,.png'}),
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
        if user and hasattr(user, 'profile'):
            pass

    def clean(self):
        cleaned_data = super().clean()
        ios_upper = cleaned_data.get('ios_upper')
        ios_lower = cleaned_data.get('ios_lower')
        if (ios_upper and not ios_lower) or (ios_lower and not ios_upper):
            raise forms.ValidationError('Both upper and lower IOS scans must be provided together.')
        return cleaned_data

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
        fields = ['sagittal_left', 'sagittal_right', 'vertical', 'transverse', 'midline']
        widgets = {
            'sagittal_left': forms.Select(attrs={'class': 'form-control'}),
            'sagittal_right': forms.Select(attrs={'class': 'form-control'}),
            'vertical': forms.Select(attrs={'class': 'form-control'}),
            'transverse': forms.Select(attrs={'class': 'form-control'}),
            'midline': forms.Select(attrs={'class': 'form-control'}),
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
        if self.instance and self.instance.pk:
            self.fields['tags_text'].initial = ', '.join(self.instance.tag_names())

        if user and hasattr(user, 'profile'):
            pass

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
