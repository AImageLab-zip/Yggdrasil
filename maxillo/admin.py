"""Maxillo's own models, and only those.

This file used to register six ``common`` models as well -- ``Modality``,
``AnnotationMethod``, ``ProjectAccess``, ``Job``, ``FileRegistry`` and
``Invitation``. Django files a model under the app that *declares* it, so they
rendered under "Common" while living here: the admin looked coherent and the
code was not, and removing ``maxillo`` from ``INSTALLED_APPS`` would have taken
half of ``common``'s admin with it. They are in :mod:`common.admin` now.

The ``ReadOnlyAdminMixin`` that thirteen of these admins inherited is gone too.
It was three methods that each returned ``super()`` -- a no-op with a name that
promised the opposite, on ``PatientAdmin`` among others. Nothing it was applied
to was ever read-only, and nothing here has become read-only by its removal;
what changed is that the class list no longer claims otherwise.
"""

from django.contrib import admin
from django.db.models import Count

from common.admin import DomainFolderAdmin, DomainProjectAdmin

from .models import (
    Classification,
    Dataset,
    Export,
    Folder,
    IntraoralToothSegmentation,
    MaxilloProject,
    Patient,
    Tag,
    VoiceCaption,
)


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin):
    # `scan_count` and `patient_count` are the same method body twice
    # (maxillo/models.py) -- two identical COUNT queries per row for one number.
    # One column, and it is annotated rather than counted per row.
    list_display = ['name', 'patient_count', 'created_at', 'created_by']
    list_filter = ['created_at']
    list_select_related = ['created_by']
    search_fields = ['name', 'description']
    readonly_fields = ['created_at']
    autocomplete_fields = ['created_by']

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_patients=Count('patients'))

    @admin.display(description="Patients", ordering="_patients")
    def patient_count(self, obj):
        return getattr(obj, "_patients", None) or obj.patients.count()


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    # `project` is shown and filterable for the same reason as on FolderAdmin: a
    # patient in the wrong project is invisible in the app but looks fine here.
    list_display = ['patient_id', 'name', 'project', 'folder', 'dataset', 'visibility', 'uploaded_at', 'uploaded_by']
    list_filter = ['project', 'visibility', 'dataset', 'uploaded_at']
    # Every FK in list_display, in the one query that fetches the page. Without
    # it a 100-row page is 400 extra queries.
    list_select_related = ['project', 'folder', 'dataset', 'uploaded_by']
    search_fields = ['patient_id', 'name']
    readonly_fields = ['patient_id', 'uploaded_at']
    autocomplete_fields = ['project', 'folder', 'dataset', 'uploaded_by']
    filter_horizontal = ['modalities', 'tags']


@admin.register(MaxilloProject)
class MaxilloProjectAdmin(DomainProjectAdmin):
    """Maxillo projects, shown under the Maxillo admin section (domain forced)."""
    domain = 'maxillo'


@admin.register(Classification)
class ClassificationAdmin(admin.ModelAdmin):
    list_display = ['id', 'patient', 'classifier', 'sagittal_left', 'sagittal_right', 'vertical', 'transverse', 'midline', 'annotator', 'timestamp']
    list_filter = ['classifier', 'sagittal_left', 'sagittal_right', 'vertical', 'transverse', 'midline', 'timestamp']
    list_select_related = ['patient', 'annotator']
    search_fields = ['patient__patient_id']
    readonly_fields = ['timestamp']
    autocomplete_fields = ['patient', 'annotator']


@admin.register(IntraoralToothSegmentation)
class IntraoralToothSegmentationAdmin(admin.ModelAdmin):
    list_display = ['id', 'patient', 'image_file', 'polygon_count', 'updated_by', 'updated_at']
    list_filter = ['updated_at', 'updated_by']
    list_select_related = ['patient', 'image_file', 'updated_by']
    search_fields = ['patient__patient_id', 'image_file__file_path']
    readonly_fields = ['created_at', 'updated_at', 'polygon_count']
    # `image_file` targets FileRegistry -- 37k rows, rendered as a full <select>
    # on every add and change form until this was here.
    autocomplete_fields = ['patient', 'image_file', 'updated_by', 'confirmed_by']

    @admin.display(description='Polygons')
    def polygon_count(self, obj):
        return sum(len(polygons) for polygons in (obj.teeth or {}).values() if isinstance(polygons, list))


@admin.register(VoiceCaption)
class VoiceCaptionAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'patient', 'modality', 'duration', 'processing_status', 'created_at']
    list_filter = ['modality', 'processing_status', 'created_at']
    list_select_related = ['user', 'patient']
    search_fields = ['=id', 'user__username', 'patient__patient_id']
    readonly_fields = ['created_at', 'updated_at']
    autocomplete_fields = ['patient', 'user']

    def get_readonly_fields(self, request, obj=None):
        if obj:  # Editing an existing object
            return self.readonly_fields + ['patient', 'user']
        return self.readonly_fields


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at']
    search_fields = ['name']


@admin.register(Folder)
class FolderAdmin(DomainFolderAdmin):
    """Maxillo folders (project picker scoped to the maxillo domain)."""
    domain = 'maxillo'


@admin.register(Export)
class ExportAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'status', 'patient_count', 'file_size_display', 'created_at', 'started_at', 'completed_at']
    list_filter = ['status', 'created_at']
    list_select_related = ['user']
    search_fields = ['user__username', 'query_summary', 'error_message']
    readonly_fields = ['created_at', 'started_at', 'completed_at', 'query_params', 'query_summary']
    autocomplete_fields = ['user']

    fieldsets = (
        ('Status', {
            'fields': ('user', 'status', 'error_message')
        }),
        ('Query', {
            'fields': ('query_params', 'query_summary')
        }),
        ('Result', {
            'fields': ('file_path', 'file_size', 'patient_count')
        }),
        ('Timing', {
            'fields': ('created_at', 'started_at', 'completed_at')
        }),
    )

    @admin.display(description="File size")
    def file_size_display(self, obj):
        """Display file size in human-readable format"""
        if obj.file_size:
            return f"{obj.file_size / (1024 * 1024):.2f} MB"
        return "-"
