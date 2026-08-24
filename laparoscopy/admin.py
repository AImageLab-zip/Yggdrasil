from django.contrib import admin
from django.utils.html import format_html

from common.annotation_lock import raw_data_is_locked

from .models import (
    Classification,
    Dataset,
    Export,
    Folder,
    LaparoscopyProject,
    Patient,
    QuadrantClassificationMarker,
    QuadrantType,
    Tag,
    VoiceCaption,
)


class QuadrantClassificationMarkerInline(admin.TabularInline):
    model = QuadrantClassificationMarker
    extra = 0
    fields = ['time_ms', 'quadrant_type', 'created_by', 'updated_by', 'updated_at']
    readonly_fields = ['created_by', 'updated_by', 'updated_at']
    ordering = ['time_ms', 'id']


@admin.register(LaparoscopyProject)
class LaparoscopyProjectAdmin(admin.ModelAdmin):
    """Laparoscopy projects, shown under the Laparoscopy admin section (domain forced)."""
    list_display = ['name', 'slug', 'icon', 'is_active', 'created_at', 'created_by']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'description', 'slug']
    prepopulated_fields = {'slug': ('name',)}
    filter_horizontal = ['modalities', 'annotation_methods', 'disabled_steps']
    readonly_fields = ['domain']

    def get_queryset(self, request):
        return super().get_queryset(request).filter(domain='laparoscopy')

    def save_model(self, request, obj, form, change):
        obj.domain = 'laparoscopy'
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
    list_display = ['patient_id', 'name', 'project', 'visibility', 'folder', 'raw_locked', 'uploaded_at', 'uploaded_by']
    list_filter = ['project', 'visibility', 'uploaded_at']
    list_select_related = ['project', 'folder']
    search_fields = ['patient_id', 'name']
    inlines = [QuadrantClassificationMarkerInline]

    # Laparoscopy is the one domain that still keeps raw scans in FileFields on
    # the patient itself rather than in FileRegistry, so the freeze has to be
    # applied here as well. The `_norm` fields are pipeline output, not raw.
    RAW_FILE_FIELDS = ['upper_scan_raw', 'lower_scan_raw', 'cbct']

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
        return raw_data_is_locked(obj)


@admin.register(Classification)
class ClassificationAdmin(admin.ModelAdmin):
    list_display = ['id', 'patient', 'classifier', 'timestamp']
    list_filter = ['classifier', 'timestamp']


@admin.register(VoiceCaption)
class VoiceCaptionAdmin(admin.ModelAdmin):
    list_display = ['id', 'patient', 'user', 'modality', 'processing_status', 'created_at']
    list_filter = ['modality', 'processing_status', 'created_at']


@admin.register(Export)
class ExportAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'status', 'patient_count', 'file_size', 'created_at', 'completed_at']
    list_filter = ['status', 'share_mode', 'created_at', 'completed_at']
    search_fields = ['id', 'user__username', 'query_summary', 'file_path', 'share_token']
    readonly_fields = ['created_at', 'started_at', 'completed_at', 'shared_at']


@admin.register(QuadrantType)
class QuadrantTypeAdmin(admin.ModelAdmin):
    list_display = ['id', 'project', 'name', 'color_preview', 'color', 'order', 'marker_count']
    list_filter = ['project']
    search_fields = ['name', 'project__name', 'project__slug']
    ordering = ['project__name', 'order', 'name']

    @admin.display(description='Color')
    def color_preview(self, obj):
        return format_html(
            '<span style="display:inline-block;width:14px;height:14px;border-radius:50%;background:{};border:1px solid #ccc;"></span>',
            obj.color,
        )

    @admin.display(description='Markers')
    def marker_count(self, obj):
        return obj.markers.count()


@admin.register(QuadrantClassificationMarker)
class QuadrantClassificationMarkerAdmin(admin.ModelAdmin):
    list_display = ['id', 'patient', 'patient_name', 'quadrant_type', 'time_seconds', 'created_by', 'updated_by', 'updated_at']
    list_filter = ['quadrant_type', 'patient__visibility', 'created_at', 'updated_at']
    search_fields = ['patient__patient_id', 'patient__name', 'quadrant_type__name']
    autocomplete_fields = ['patient', 'quadrant_type', 'created_by', 'updated_by']
    ordering = ['patient_id', 'time_ms', 'id']

    @admin.display(description='Patient Name')
    def patient_name(self, obj):
        return obj.patient.name

    @admin.display(description='Time (s)', ordering='time_ms')
    def time_seconds(self, obj):
        return f'{obj.time_ms / 1000:.3f}'
