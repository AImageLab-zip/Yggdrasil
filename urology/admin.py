from django.contrib import admin

from common.admin import DomainFolderAdmin, DomainProjectAdmin

from .models import Export, Folder, Patient, Tag, UrologyProject, VoiceCaption


@admin.register(UrologyProject)
class UrologyProjectAdmin(DomainProjectAdmin):
    """Urology projects, shown under the Urology admin section (domain forced)."""
    domain = "urology"


@admin.register(Folder)
class FolderAdmin(DomainFolderAdmin):
    """Urology folders (project picker scoped to the urology domain)."""
    domain = "urology"


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ["name", "created_at"]
    search_fields = ["name"]


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = [
        "patient_id",
        "name",
        "project",
        "folder",
        "visibility",
        "uploaded_at",
        "uploaded_by",
    ]
    list_filter = ["project", "visibility", "uploaded_at"]
    list_select_related = ["project", "folder", "uploaded_by"]
    search_fields = ["patient_id", "name"]
    autocomplete_fields = ["project", "folder", "uploaded_by"]
    filter_horizontal = ["modalities", "tags"]


@admin.register(VoiceCaption)
class VoiceCaptionAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "patient",
        "user",
        "modality",
        "processing_status",
        "created_at",
    ]
    list_filter = ["modality", "processing_status", "created_at"]
    list_select_related = ["patient", "user"]
    search_fields = ["=id", "user__username", "patient__patient_id"]
    autocomplete_fields = ["patient", "user"]


@admin.register(Export)
class ExportAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "user",
        "status",
        "patient_count",
        "created_at",
        "completed_at",
    ]
    list_filter = ["status", "created_at"]
    list_select_related = ["user"]
    search_fields = ["=id", "user__username", "query_summary", "file_path"]
    autocomplete_fields = ["user"]
