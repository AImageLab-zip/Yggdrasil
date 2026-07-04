from django.contrib import admin

from common.models import ModalityProcessingConfig, SystemCheck


@admin.register(ModalityProcessingConfig)
class ModalityProcessingConfigAdmin(admin.ModelAdmin):
    list_display = ("modality", "requires_processing", "queue_name", "is_blocking", "is_enabled", "updated_at")
    list_filter = ("requires_processing", "is_blocking", "is_enabled")
    search_fields = ("modality__name", "modality__slug", "queue_name")
    autocomplete_fields = ("modality",)
    filter_horizontal = ("depends_on",)
    readonly_fields = ("updated_at",)


@admin.register(SystemCheck)
class SystemCheckAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "ran_at", "duration_ms")
    list_filter = ("name", "status")
    date_hierarchy = "ran_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
