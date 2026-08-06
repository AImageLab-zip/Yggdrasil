from django.contrib import admin

from .models import BrainProject, Dataset, Export, Folder, Patient, Tag, VoiceCaption


@admin.register(BrainProject)
class BrainProjectAdmin(admin.ModelAdmin):
    """Brain projects, shown under the Brain admin section (domain forced)."""
    list_display = ['name', 'slug', 'icon', 'is_active', 'created_at', 'created_by']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'description', 'slug']
    prepopulated_fields = {'slug': ('name',)}
    filter_horizontal = ['modalities', 'annotation_methods', 'disabled_steps']
    readonly_fields = ['domain']

    def get_queryset(self, request):
        return super().get_queryset(request).filter(domain='brain')

    def save_model(self, request, obj, form, change):
        obj.domain = 'brain'
        super().save_model(request, obj, form, change)


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'created_by']
    search_fields = ['name', 'description']


@admin.register(Folder)
class FolderAdmin(admin.ModelAdmin):
    list_display = ['name', 'parent', 'is_demo', 'created_at', 'created_by']
    list_filter = ['is_demo']
    list_editable = ['is_demo']
    search_fields = ['name']


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at']
    search_fields = ['name']


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ['patient_id', 'name', 'visibility', 'uploaded_at', 'uploaded_by']
    list_filter = ['visibility', 'uploaded_at']
    search_fields = ['patient_id', 'name']


@admin.register(VoiceCaption)
class VoiceCaptionAdmin(admin.ModelAdmin):
    list_display = ['id', 'patient', 'user', 'modality', 'processing_status', 'created_at']
    list_filter = ['modality', 'processing_status', 'created_at']


@admin.register(Export)
class ExportAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'status', 'patient_count', 'created_at', 'completed_at']
    list_filter = ['status', 'created_at']
