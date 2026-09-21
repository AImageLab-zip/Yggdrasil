from django.contrib import admin

from common.admin import DomainFolderAdmin, DomainProjectAdmin

from .models import (
    CardiologyProject,
    Classification,
    Export,
    Folder,
    Patient,
    Tag,
    VoiceCaption,
)


@admin.register(CardiologyProject)
class CardiologyProjectAdmin(DomainProjectAdmin):
    """Cardiology projects, shown under the Cardiology admin section (domain forced)."""
    domain = 'cardiology'


@admin.register(Folder)
class FolderAdmin(DomainFolderAdmin):
    """Cardiology folders (project picker scoped to the cardiology domain)."""
    domain = 'cardiology'


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at']
    search_fields = ['name']


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ['patient_id', 'name', 'project', 'visibility', 'folder', 'uploaded_at', 'uploaded_by']
    list_filter = ['project', 'visibility', 'uploaded_at']
    list_select_related = ['project', 'folder', 'uploaded_by']
    search_fields = ['patient_id', 'name']
    autocomplete_fields = ['project', 'folder', 'uploaded_by']
    filter_horizontal = ['modalities', 'tags']


@admin.register(Classification)
class ClassificationAdmin(admin.ModelAdmin):
    list_display = ['id', 'patient', 'classifier', 'value', 'annotator', 'timestamp']
    list_filter = ['classifier', 'value', 'timestamp']
    list_select_related = ['patient', 'annotator']
    search_fields = ['patient__patient_id', 'patient__name']
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
