from django.contrib import admin

from common.admin import DomainFolderAdmin, DomainProjectAdmin

from .models import BrainProject, Dataset, Export, Folder, Patient, Tag, VoiceCaption


@admin.register(BrainProject)
class BrainProjectAdmin(DomainProjectAdmin):
    """Brain projects, shown under the Brain admin section (domain forced)."""
    domain = 'brain'


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'created_by']
    search_fields = ['name', 'description']


@admin.register(Folder)
class FolderAdmin(DomainFolderAdmin):
    """Brain folders (project picker scoped to the brain domain)."""
    domain = 'brain'


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
