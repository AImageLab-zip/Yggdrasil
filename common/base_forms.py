"""The patient forms every domain shares.

`PatientForm`, `PatientUploadForm` and `PatientManagementForm` were written once
and then copied into each domain app, where they diverged only in the domain
constant, a placeholder and a label. `common/base_models.py` is the precedent:
this is its forms half.

Built by a factory rather than subclassed because a ModelForm binds its model in
`Meta`, so each domain needs its own classes; what it does not need is its own
copy of the queryset scoping, the folder/project cross-check and the tag parsing.

A domain with extra upload fields passes them in ``upload_extra_fields``.
"""

from django import forms

from common.models import Project, ProjectAccess
from common.permissions import filter_folders_for_user

DEFAULT_TAGS_PLACEHOLDER = "e.g. caseA, urgent"


def build_patient_forms(
    *,
    domain,
    patient_model,
    folder_model,
    tag_model,
    name_label="Scan Name",
    tags_placeholder=DEFAULT_TAGS_PLACEHOLDER,
    upload_extra_fields=None,
):
    """Return ``(PatientForm, PatientUploadForm, PatientManagementForm)`` for a domain.

    ``upload_extra_fields`` is a mapping of extra form fields to add to the upload
    form -- a domain's modality file inputs, which are the one part of these forms
    that is genuinely its own.
    """

    class PatientForm(forms.ModelForm):
        class Meta:
            model = patient_model
            fields = []

    upload_attrs = {
        "project": forms.ModelChoiceField(
            queryset=Project.objects.none(),
            required=True,
            label="Project",
            widget=forms.Select(attrs={"class": "form-control"}),
        ),
        "folder": forms.ModelChoiceField(
            queryset=folder_model.objects.none(),
            required=True,
            label="Folder",
            widget=forms.Select(attrs={"class": "form-control"}),
        ),
        "tags_text": forms.CharField(
            required=False,
            help_text="Comma-separated tags",
            widget=forms.TextInput(
                attrs={"class": "form-control", "placeholder": tags_placeholder}
            ),
        ),
    }
    upload_attrs.update(upload_extra_fields or {})

    class _UploadMeta:
        model = patient_model
        fields = ["name", "project", "folder"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Patient X"}
            ),
        }
        labels = {"name": name_label, "folder": "Folder"}

    # Bound here, under a name the __init__ signature cannot shadow: that signature
    # has to accept a `domain` kwarg (maxillo's shared upload view passes the
    # namespace it is serving), and a parameter of that name would mask this one --
    # silently filtering projects by None and offering an empty list.
    bound_domain = domain

    def _upload_init(self, *args, user=None, current_project=None, domain=None, **kwargs):
        # `domain` is accepted and ignored: the factory already bound it when it
        # built this class, and that binding is the authoritative one.
        forms.ModelForm.__init__(self, *args, **kwargs)
        self.fields["folder"].required = True
        if not user:
            self.fields["project"].queryset = Project.objects.none()
            self.fields["folder"].queryset = folder_model.objects.none()
            return

        projects_qs = Project.objects.filter(domain=bound_domain, is_active=True)
        if not user.is_staff:
            accessible = ProjectAccess.objects.filter(user=user).values_list(
                "project_id", flat=True
            )
            projects_qs = projects_qs.filter(id__in=accessible)
        self.fields["project"].queryset = projects_qs.order_by("name")

        project_id = None
        if self.data:
            project_id = self.data.get("project") or self.data.get("project_id")
        if not project_id and current_project:
            project_id = getattr(current_project, "id", current_project)
        if project_id:
            self.fields["folder"].queryset = folder_model.objects.filter(
                project_id=project_id, parent__isnull=True
            ).order_by("name")
            self.fields["project"].initial = project_id

    def _upload_clean(self):
        cleaned_data = forms.ModelForm.clean(self)
        project = cleaned_data.get("project")
        folder = cleaned_data.get("folder")
        if project and folder and folder.project_id != project.id:
            raise forms.ValidationError(
                "The selected folder does not belong to the selected project."
            )
        if not folder:
            raise forms.ValidationError("A folder is required.")
        return cleaned_data

    def _upload_save(self, commit=True):
        instance = forms.ModelForm.save(self, commit)
        project = self.cleaned_data.get("project")
        if project:
            instance.project = project
        tag_names = _tag_names(self.cleaned_data.get("tags_text"))
        if commit and tag_names:
            instance.tags.set(
                _tags(tag_model, tag_names) + list(instance.tags.all())
            )
        return instance

    PatientUploadForm = type(
        "PatientUploadForm",
        (forms.ModelForm,),
        {
            **upload_attrs,
            "Meta": _UploadMeta,
            "__init__": _upload_init,
            "clean": _upload_clean,
            "save": _upload_save,
        },
    )

    class PatientManagementForm(forms.ModelForm):
        folder = forms.ModelChoiceField(
            queryset=folder_model.objects.all().order_by("name"),
            required=False,
            widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
        )
        tags_text = forms.CharField(
            required=False,
            help_text="Comma-separated tags",
            widget=forms.TextInput(
                attrs={
                    "class": "form-control form-control-sm",
                    "placeholder": tags_placeholder,
                }
            ),
        )

        class Meta:
            model = patient_model
            fields = ["name", "folder"]
            widgets = {
                "name": forms.TextInput(
                    attrs={
                        "class": "form-control form-control-sm",
                        "placeholder": "Scan name",
                    }
                ),
            }
            labels = {"name": "Name", "folder": "Folder"}

        def __init__(self, *args, user=None, **kwargs):
            super().__init__(*args, **kwargs)
            scope_project_id = getattr(self.instance, "project_id", None)
            if user:
                folders_qs = folder_model.objects.filter(
                    parent__isnull=True
                ).order_by("name")
                if scope_project_id:
                    folders_qs = folders_qs.filter(project_id=scope_project_id)
                self.fields["folder"].queryset = filter_folders_for_user(
                    user, folders_qs, domain
                )
            else:
                self.fields["folder"].queryset = folder_model.objects.none()
            if self.instance and self.instance.pk:
                self.fields["tags_text"].initial = ", ".join(self.instance.tag_names())

        def clean(self):
            cleaned_data = super().clean()
            name = cleaned_data.get("name")
            if name and len(name.strip()) == 0:
                raise forms.ValidationError("Patient name cannot be empty.")
            return cleaned_data

        def save(self, commit=True):
            instance = super().save(commit)
            tag_names = _tag_names(self.cleaned_data.get("tags_text"))
            if commit:
                instance.tags.set(_tags(tag_model, tag_names))
            return instance

    return PatientForm, PatientUploadForm, PatientManagementForm


def _tag_names(tags_text):
    return [tag.strip() for tag in (tags_text or "").split(",") if tag.strip()]


def _tags(tag_model, names):
    return [tag_model.objects.get_or_create(name=name)[0] for name in names]
