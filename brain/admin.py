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
    # See maxillo.FolderAdmin: `project` is shown so a mis-filed folder is visible.
    list_display = ['name', 'project', 'parent', 'is_demo', 'created_at', 'created_by']
    list_filter = ['project', 'is_demo']
    list_editable = ['is_demo']
    list_select_related = ['project', 'parent']
    search_fields = ['name']


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at']
    search_fields = ['name']


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    # See maxillo.PatientAdmin: `project` is shown so a mis-filed patient is visible.
    list_display = ['patient_id', 'name', 'project', 'folder', 'visibility', 'uploaded_at', 'uploaded_by']
    list_filter = ['project', 'visibility', 'uploaded_at']
    list_select_related = ['project', 'folder']
    search_fields = ['patient_id', 'name']


@admin.register(VoiceCaption)
class VoiceCaptionAdmin(admin.ModelAdmin):
    list_display = ['id', 'patient', 'user', 'modality', 'processing_status', 'created_at']
    list_filter = ['modality', 'processing_status', 'created_at']


@admin.register(Export)
class ExportAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'status', 'patient_count', 'created_at', 'completed_at']
    list_filter = ['status', 'created_at']
