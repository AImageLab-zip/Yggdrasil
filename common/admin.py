from django.contrib import admin

from common.models import ProcessingStep, SystemCheck


@admin.register(ProcessingStep)
class ProcessingStepAdmin(admin.ModelAdmin):
    list_display = ("modality", "name", "slug", "inputs", "queue_name", "algo_name", "is_enabled", "is_blocking", "discard_raw", "updated_at")
    list_filter = ("is_enabled", "is_blocking", "discard_raw", "modality")
    list_editable = ("is_enabled", "queue_name", "algo_name", "discard_raw")
    search_fields = ("name", "slug", "modality__name", "modality__slug", "queue_name")
    autocomplete_fields = ("modality",)
    filter_horizontal = ("depends_on",)
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("updated_at",)

    @admin.display(description="Inputs")
    def inputs(self, obj):
        return ", ".join(s.slug for s in obj.depends_on.all()) or "—"


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
