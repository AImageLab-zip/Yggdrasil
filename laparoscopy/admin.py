from django.contrib import admin
from django.utils.html import format_html

from .models import (
    Classification,
    Dataset,
    Export,
    Folder,
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


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at', 'created_by']
    search_fields = ['name', 'description']


@admin.register(Folder)
class FolderAdmin(admin.ModelAdmin):
    list_display = ['name', 'parent', 'created_at', 'created_by']
    search_fields = ['name']


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ['name', 'created_at']
    search_fields = ['name']


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ['patient_id', 'name', 'visibility', 'folder', 'uploaded_at', 'uploaded_by']
    list_filter = ['visibility', 'uploaded_at']
    search_fields = ['patient_id', 'name']
    inlines = [QuadrantClassificationMarkerInline]


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
