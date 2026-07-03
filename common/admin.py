from django.contrib import admin

from common.models import SystemCheck


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
