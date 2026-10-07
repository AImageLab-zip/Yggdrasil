from django.contrib import admin

from common.admin import DomainFolderAdmin, DomainProjectAdmin, raw_locked_changelist
from common.annotation_lock import raw_data_is_locked

from .models import (
    Classification,
    Export,
    Folder,
    LaparoscopyProject,
    Patient,
    Tag,
    VoiceCaption,
)


@admin.register(LaparoscopyProject)
class LaparoscopyProjectAdmin(DomainProjectAdmin):
    """Laparoscopy projects, shown under the Laparoscopy admin section (domain forced)."""
    domain = 'laparoscopy'


@admin.register(Folder)
class FolderAdmin(DomainFolderAdmin):
    """Laparoscopy folders (project picker scoped to the laparoscopy domain)."""
    domain = 'laparoscopy'


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at']
    search_fields = ['name']


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    # See maxillo.PatientAdmin: `project` is shown so a mis-filed patient is visible.
    list_display = ['patient_id', 'name', 'project', 'visibility', 'folder', 'raw_locked', 'uploaded_at', 'uploaded_by']
    list_filter = ['project', 'visibility', 'uploaded_at']
    list_select_related = ['project', 'folder', 'uploaded_by']
    search_fields = ['patient_id', 'name']
    autocomplete_fields = ['project', 'folder', 'uploaded_by']
    filter_horizontal = ['modalities', 'tags']

    # Laparoscopy is the one domain that still keeps raw scans in FileFields on
    # the patient itself rather than in FileRegistry, so the freeze has to be
    # applied here as well. The `_norm` fields are pipeline output, not raw.
    RAW_FILE_FIELDS = ['upper_scan_raw', 'lower_scan_raw', 'cbct']

    def get_changelist(self, request, **kwargs):
        # `raw_locked` is up to six queries per row asked one row at a time;
        # this resolves the page in a fixed number. See common.admin.raw_lock_map.
        return raw_locked_changelist(lambda patient: patient)

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        # Superusers keep the admin-side override for genuine data repair.
        if obj is None or request.user.is_superuser:
            return readonly
        if raw_data_is_locked(obj):
            readonly += [f for f in self.RAW_FILE_FIELDS if f not in readonly]
        return readonly

    @admin.display(description='Raw locked', boolean=True)
    def raw_locked(self, obj):
        cached = getattr(obj, '_raw_locked', None)
        if cached is not None:
            return cached
        return raw_data_is_locked(obj)


@admin.register(Classification)
class ClassificationAdmin(admin.ModelAdmin):
    list_display = ['id', 'patient', 'classifier', 'annotator', 'timestamp']
    list_filter = ['classifier', 'timestamp']
    list_select_related = ['patient', 'annotator']
    search_fields = ['patient__patient_id', 'patient__name', 'notes']
    autocomplete_fields = ['patient', 'annotator']


@admin.register(VoiceCaption)
class VoiceCaptionAdmin(admin.ModelAdmin):
    list_display = ['id', 'patient', 'user', 'modality', 'processing_status', 'created_at']
    list_filter = ['modality', 'processing_status', 'created_at']
    list_select_related = ['patient', 'user']
    search_fields = ['=id', 'user__username', 'patient__patient_id']
    autocomplete_fields = ['patient', 'user']


@admin.register(Export)
class ExportAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'status', 'patient_count', 'file_size', 'created_at', 'completed_at']
    list_filter = ['status', 'share_mode', 'created_at', 'completed_at']
    list_select_related = ['user']
    search_fields = ['id', 'user__username', 'query_summary', 'file_path', 'share_token']
    readonly_fields = ['created_at', 'started_at', 'completed_at', 'shared_at']
    autocomplete_fields = ['user']
