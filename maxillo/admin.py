from django.contrib import admin
from django.contrib.auth.models import User
from .models import Dataset, Patient, Classification, VoiceCaption, Export, IntraoralToothSegmentation
from common.models import Project, Modality, ProjectAccess, Job, FileRegistry, Invitation
from .models import Tag, Folder


class ReadOnlyAdminMixin:
    """Standard admin permissions."""
    
    def has_add_permission(self, request):
        return super().has_add_permission(request)
    
    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj)
    
    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj)


@admin.register(Dataset)
class DatasetAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['name', 'scan_count', 'patient_count', 'created_at', 'created_by']
    list_filter = ['created_at']
    search_fields = ['name', 'description']
    readonly_fields = ['created_at', 'scan_count', 'patient_count']


@admin.register(Patient)
class PatientAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['patient_id', 'name', 'dataset', 'visibility', 'uploaded_at', 'uploaded_by']
    list_filter = ['visibility', 'dataset', 'uploaded_at']
    search_fields = ['patient_id', 'name']
    readonly_fields = ['patient_id', 'uploaded_at']
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs


@admin.register(Project)
class ProjectAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['name', 'slug', 'icon', 'is_active', 'created_at', 'created_by']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'description', 'slug']
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ['modalities']


@admin.register(Modality)
class ModalityAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['name', 'slug', 'label', 'icon', 'is_active', 'created_at', 'created_by']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'description', 'slug', 'label', 'icon']
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = []


@admin.register(ProjectAccess)
class ProjectAccessAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['user', 'project', 'role', 'created_at']
    list_filter = ['role', 'created_at']
    search_fields = ['user__username', 'project__name']


@admin.register(Classification)
class ClassificationAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['id', 'patient', 'classifier', 'sagittal_left', 'sagittal_right', 'vertical', 'transverse', 'midline', 'annotator', 'timestamp']
    list_filter = ['classifier', 'sagittal_left', 'sagittal_right', 'vertical', 'transverse', 'midline', 'timestamp']
    search_fields = ['patient__patient_id']
    readonly_fields = ['timestamp']
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs


@admin.register(IntraoralToothSegmentation)
class IntraoralToothSegmentationAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['id', 'patient', 'image_file', 'polygon_count', 'updated_by', 'updated_at']
    list_filter = ['updated_at', 'updated_by']
    search_fields = ['patient__patient_id', 'image_file__id']
    readonly_fields = ['created_at', 'updated_at', 'polygon_count']

    def polygon_count(self, obj):
        return sum(len(polygons) for polygons in (obj.teeth or {}).values() if isinstance(polygons, list))
    polygon_count.short_description = 'Polygons'

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('patient', 'image_file', 'updated_by')
        return qs


@admin.register(VoiceCaption)
class VoiceCaptionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['id', 'user', 'patient', 'modality', 'duration', 'processing_status', 'created_at']
    list_filter = ['modality', 'processing_status', 'created_at']
    search_fields = ['user__username', 'patient__patient_id']
    readonly_fields = ['created_at', 'updated_at']
    
    def get_readonly_fields(self, request, obj=None):
        if obj:  # Editing an existing object
            return self.readonly_fields + ['patient', 'user']
        return self.readonly_fields
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs


@admin.register(Job)
class JobAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['id', 'modality_slug', 'status', 'patient', 'voice_caption', 'priority', 'dependencies_count', 'created_at', 'started_at', 'completed_at', 'retry_count']
    list_filter = ['modality_slug', 'status', 'created_at', 'started_at', 'completed_at', 'priority', ('dependencies', admin.EmptyFieldListFilter)]
    search_fields = ['patient__patient_id', 'voice_caption__id', 'worker_id']
    readonly_fields = ['created_at', 'started_at', 'completed_at', 'dependencies_list']
    
    fieldsets = (
        ('Job Information', {
            'fields': ('modality_slug', 'status', 'priority', 'patient', 'voice_caption')
        }),
        ('Dependencies', {
            'fields': ('dependencies', 'dependencies_list'),
            'description': 'Jobs that must complete before this job can start'
        }),
        ('Files & Processing', {
            'fields': ('input_file_path', 'output_files')
        }),
        ('Timing', {
            'fields': ('created_at', 'started_at', 'completed_at')
        }),
        ('Error Handling', {
            'fields': ('retry_count', 'max_retries', 'error_logs')
        }),
        ('Worker Info', {
            'fields': ('worker_id',)
        }),
    )
    
    def get_readonly_fields(self, request, obj=None):
        if obj and obj.status in ['processing', 'completed']:
            # Prevent editing jobs that are being processed or completed
            return self.readonly_fields + ['modality_slug', 'patient', 'voice_caption', 'input_file_path']
        return self.readonly_fields
    
    def dependencies_count(self, obj):
        """Display the number of dependencies for this job"""
        count = obj.dependencies.count()
        if count == 0:
            return "-"
        return f"{count} dep(s)"
    dependencies_count.short_description = "Dependencies"
    
    def dependencies_list(self, obj):
        """Display a list of dependency job IDs"""
        deps = obj.dependencies.all()[:3]  # Show first 3 dependencies
        if not deps:
            return "-"
        dep_ids = [f"#{dep.id}" for dep in deps]
        if obj.dependencies.count() > 3:
            dep_ids.append(f"... (+{obj.dependencies.count() - 3} more)")
        return ", ".join(dep_ids)
    dependencies_list.short_description = "Dependency Jobs"
    
    def get_queryset(self, request):
        """Optimize queryset to include dependencies count."""
        qs = super().get_queryset(request).prefetch_related('dependencies')
        return qs
    
    def get_fieldsets(self, request, obj=None):
        """Customize fieldsets based on job status"""
        fieldsets = list(super().get_fieldsets(request, obj))
        
        # Add dependent jobs info if this job has dependents
        if obj and obj.dependent_jobs.exists():
            dependent_info = {
                'fields': (),
                'description': f'This job has {obj.dependent_jobs.count()} dependent job(s) waiting for it to complete'
            }
            fieldsets.append(('Dependent Jobs', dependent_info))
        
        return fieldsets
    
    actions = ['retry_failed_jobs', 'cancel_pending_jobs', 'check_dependencies', 'clear_dependencies']
    
    def retry_failed_jobs(self, request, queryset):
        count = 0
        for job in queryset.filter(status='failed'):
            if job.can_retry():
                job.status = 'retrying'
                job.save()
                count += 1
        self.message_user(request, f'Retried {count} failed job(s).')
    retry_failed_jobs.short_description = "Retry selected failed jobs"
    
    def cancel_pending_jobs(self, request, queryset):
        count = queryset.filter(status__in=['pending', 'retrying']).update(status='failed')
        self.message_user(request, f'Marked {count} pending job(s) as failed.')
    cancel_pending_jobs.short_description = "Mark selected pending jobs as failed"
    
    def check_dependencies(self, request, queryset):
        """Check and update dependency status for selected jobs"""
        count = 0
        for job in queryset:
            if job.update_status_based_on_dependencies():
                count += 1
        self.message_user(request, f'Updated dependency status for {count} job(s).')
    check_dependencies.short_description = "Check and update dependency status"
    
    def clear_dependencies(self, request, queryset):
        """Clear all dependencies for selected jobs"""
        count = 0
        for job in queryset:
            if job.dependencies.exists():
                job.dependencies.clear()
                job.update_status_based_on_dependencies()
                count += 1
        self.message_user(request, f'Cleared dependencies for {count} job(s).')
    clear_dependencies.short_description = "Clear all dependencies"


@admin.register(FileRegistry)
class FileRegistryAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):  
    list_display = ['id', 'file_type', 'patient', 'voice_caption', 'file_size_mb', 'created_at', 'modality']
    list_filter = ['file_type', 'created_at']
    search_fields = ['file_path', 'patient__patient_id', 'voice_caption__id']
    readonly_fields = ['created_at', 'file_hash', 'file_size', 'file_size_mb']
    
    fieldsets = (
        ('File Information', {
            'fields': ('file_type', 'file_path', 'file_size', 'file_size_mb', 'file_hash', 'modality')
        }),
        ('Related Objects', {
            'fields': ('patient', 'voice_caption', 'processing_job')
        }),
        ('Metadata', {
            'fields': ('metadata', 'created_at')
        }),
    )
    
    def file_size_mb(self, obj):
        """Display file size in MB"""
        if obj.file_size:
            return f"{obj.file_size / (1024 * 1024):.2f} MB"
        return "-"
    file_size_mb.short_description = "File Size (MB)"
    
    def get_readonly_fields(self, request, obj=None):
        if obj:  # Editing existing object
            return self.readonly_fields + ['file_type', 'file_path', 'patient', 'voice_caption', 'processing_job']
        return self.readonly_fields
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs



@admin.register(Invitation)
class InvitationAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['code', 'email', 'role', 'project_list', 'created_by', 'created_at', 'expires_at', 'used_at', 'used_by']
    list_filter = ['role', 'created_at', 'expires_at']
    search_fields = ['code', 'email', 'created_by__username', 'used_by__username']
    readonly_fields = ['code', 'created_at', 'used_at', 'used_by']

    def project_list(self, obj):
        return ', '.join(obj.projects.values_list('name', flat=True))
    project_list.short_description = 'Projects'

    def get_readonly_fields(self, request, obj=None):
        if obj:  # Editing existing object
            return self.readonly_fields + ['created_by']
        return self.readonly_fields


@admin.register(Tag)
class TagAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['name', 'created_at']
    search_fields = ['name']


@admin.register(Folder)
class FolderAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['name', 'parent', 'created_at', 'created_by']
    search_fields = ['name']
    list_filter = ['created_at']


@admin.register(Export)
class ExportAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['id', 'user', 'status', 'patient_count', 'file_size_display', 'created_at', 'started_at', 'completed_at']
    list_filter = ['status', 'created_at']
    search_fields = ['user__username', 'query_summary', 'error_message']
    readonly_fields = ['created_at', 'started_at', 'completed_at', 'query_params', 'query_summary']

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

    def file_size_display(self, obj):
        """Display file size in human-readable format"""
        if obj.file_size:
            size_mb = obj.file_size / (1024 * 1024)
            return f"{size_mb:.2f} MB"
        return "-"
    file_size_display.short_description = "File size"
