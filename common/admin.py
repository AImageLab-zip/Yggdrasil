import functools

from django.apps import apps
from django.contrib import admin
from django.db.models import Q
from django.utils.html import format_html

from common.deletion import deletion_summary, delete_project
from common.models import (
    AnnotationMethod,
    Modality,
    ProcessingStep,
    Project,
    ProjectAccess,
    SiteMaintenance,
    SystemCheck,
)


def _stamp_author(request, obj):
    """Record who created ``obj``, if the request says.

    ``created_by`` is a nullable audit column: no author is a valid state, and
    guessing one would be worse than leaving it empty. Anonymous and
    request-less callers (a management command, a direct ``save_model``) are
    therefore skipped rather than refused.
    """
    if obj.created_by_id is not None:
        return
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        obj.created_by = user


class DomainProjectAdmin(admin.ModelAdmin):
    """A Project admin scoped to one domain, shared by the three proxy models.

    ``MaxilloProject``, ``BrainProject`` and ``LaparoscopyProject`` are proxies of
    the same table differing only in a domain slug, so the three admins that
    served them were three copies of the same class. They are one class here, and
    ``domain`` is the only thing a subclass supplies.

    **What it fixes.** A project could be given any modality, any annotation
    method and any processing step in the database -- bite classification on a
    brain project, video on a maxillo one -- because nothing consulted
    ``AnnotationMethod.domain``, which has existed since migration 0043, and
    ``Modality`` had no domain at all until 0048. Every M2M picker is now filtered
    to *this* domain plus the domain-blank rows, which is the same
    ``Q(domain=X) | Q(domain="")`` rule ``0043_backfill_project_domain_and_roles``
    already uses to decide what a project starts with.

    Filtering the *queryset* rather than hiding options in JavaScript is what
    makes this hold on save as well as on render: ``ModelMultipleChoiceField``
    validates submitted ids against exactly this queryset, so a cross-domain id
    posted by hand is refused. And because the domain belongs to the admin class
    rather than to the object, it is known on the **add** form too -- the case a
    ``get_object(request)``-based filter cannot serve, and the case the bug was
    reported against.

    A ``ProcessingStep`` has no domain of its own; it is its modality's.

    **Who may.** A project is the top of the ownership tree -- folders,
    patients, files and annotations all hang off it -- so creating and
    destroying one is a superuser act. Editing an existing project (its
    modalities, its enabled methods) stays with the ordinary
    ``common.change_project`` permission.

    **Creating one.** ``created_by`` is filled from the request. It is a
    ``null=True`` audit column, but it was not ``blank=True``, so the admin's
    add form demanded a user be picked by hand and refused the form when none
    was -- which is why "New project" on the control panel could not create
    anything.

    **Deleting one.** Django's own delete cannot: the annotation graph PROTECTs
    the files it was drawn on, and PROTECT raises even inside the same cascade,
    so the confirmation page turned into "cannot be deleted" and the POST was a
    silent no-op. Both the page and the delete go through
    :mod:`common.deletion`, which takes the tree down in dependency
    order. The confirmation therefore states real counts -- including the
    annotation items and payloads that will be destroyed -- rather than the
    list Django could not build.
    """

    #: The domain slug this admin serves. Subclasses must set it.
    domain = None

    list_display = ['name', 'slug', 'icon', 'is_active', 'created_at', 'created_by']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'description', 'slug']
    prepopulated_fields = {'slug': ('name',)}
    filter_horizontal = ['modalities', 'annotation_methods', 'disabled_steps']
    readonly_fields = ['domain']

    def get_queryset(self, request):
        return super().get_queryset(request).filter(domain=self.domain)

    def get_inlines(self, request, obj):
        # Access first: it is the thing you come to a project page to change,
        # and since the app no longer offers a per-folder access dialog, this is
        # where access is granted. Then the folders, a sub-organization of the
        # project, edited where the project is. The folder inline omits
        # `parent`: folders are created flat by the app (`create_folder` is
        # single-level), and a picker here would list every folder in the
        # domain, across projects.
        return [ProjectAccessInline, _folder_inline_for(self.domain)]

    def has_add_permission(self, request):
        return request.user.is_superuser and super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser and super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        obj.domain = self.domain
        if not change:
            _stamp_author(request, obj)
        super().save_model(request, obj, form, change)

    def get_deleted_objects(self, objs, request):
        """Say what the delete destroys, since Django's collector cannot.

        Returns Django's 4-tuple ``(to_delete, model_count, perms_needed,
        protected)``. ``protected`` is empty by construction: the PROTECTed rows
        are not obstacles here, they are part of what gets deleted, and listing
        them as protected is what produced the dead-end page.
        """
        lines = []
        totals = {}
        for project in objs:
            summary = deletion_summary(project)
            for key, value in summary.items():
                totals[key] = totals.get(key, 0) + value
            lines.append(
                format_html(
                    "<strong>{}</strong>: {}",
                    project.name,
                    ", ".join(
                        f"{count} {label.replace('_', ' ')}"
                        for label, count in summary.items()
                    ),
                )
            )
        if totals.get("annotation_items") or totals.get("annotation_payloads"):
            lines.append(
                format_html(
                    "<strong>{}</strong>",
                    "This permanently destroys annotation work and the files it "
                    "was drawn on. It cannot be undone.",
                )
            )
        model_count = {
            label.replace("_", " "): count for label, count in totals.items() if count
        }
        return lines, model_count, set(), []

    def delete_model(self, request, obj):
        delete_project(obj)

    def delete_queryset(self, request, queryset):
        for project in queryset:
            delete_project(project)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if 'domain' in form.base_fields:
            form.base_fields['domain'].initial = self.domain
        return form

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        here = Q(domain=self.domain) | Q(domain='')
        if db_field.name == 'modalities':
            kwargs['queryset'] = Modality.objects.filter(here)
        elif db_field.name == 'annotation_methods':
            kwargs['queryset'] = AnnotationMethod.objects.filter(here)
        elif db_field.name == 'disabled_steps':
            kwargs['queryset'] = ProcessingStep.objects.filter(
                Q(modality__domain=self.domain) | Q(modality__domain='')
            ).select_related('modality')
        return super().formfield_for_manytomany(db_field, request, **kwargs)


class ProjectAccessInline(admin.TabularInline):
    """Who may use this project, edited on the project itself.

    ``ProjectAccess`` *is* the access model -- the only one authorization reads
    (``filter_folders_for_user`` and ``filter_patients_for_user`` scope by
    project id and nothing else). Granting it was previously possible only from
    the app's per-folder dialog, which promised a granularity that does not
    exist, or from the standalone ProjectAccess changelist, where you pick the
    project from a dropdown of every project in the database. Here the project
    is the page you are on.
    """

    model = ProjectAccess
    fields = ["user", "role", "created_at"]
    readonly_fields = ["created_at"]
    autocomplete_fields = ["user"]
    extra = 1
    verbose_name = "user access"
    verbose_name_plural = "Users with access to this project"


class FolderInlineBase(admin.TabularInline):
    """Folders shown inside their project. Model supplied by the subclass."""

    fields = ["name", "is_demo", "created_by", "created_at"]
    readonly_fields = ["created_at"]
    extra = 0
    show_change_link = True
    verbose_name_plural = "Folders in this project"


@functools.lru_cache(maxsize=None)
def _folder_inline_for(domain):
    """The ``FolderInlineBase`` subclass bound to ``domain``'s Folder model.

    Built once per domain rather than declared three times: the only thing that
    differs between the three is the model, exactly as with the project admins.
    """
    return type(
        f"{domain.capitalize()}FolderInline",
        (FolderInlineBase,),
        {"model": apps.get_model(domain, "Folder")},
    )


class DomainFolderAdmin(admin.ModelAdmin):
    """A Folder admin scoped to one domain, shared by the three Folder models.

    Folders are a sub-organization *inside* a project -- not the same kind of
    thing as a project, which is what the admin used to suggest by listing them
    side by side with an unscoped project picker. That picker offered every
    project in the database, so a maxillo folder could be filed under a brain
    project: invisible in the app, and pointing at patients in another domain's
    tables. It is scoped here the same way ``DomainProjectAdmin`` scopes its
    M2M pickers, and for the same reason -- filtering the queryset, not the
    rendered options, is what also holds on save.

    Creating folders is left to the app (``create_folder``, project admins
    only); this admin exists to see and repair mis-filed ones.
    """

    #: The domain slug this admin serves. Subclasses must set it.
    domain = None

    list_display = ["name", "project", "parent", "is_demo", "created_at", "created_by"]
    list_editable = ["is_demo"]
    list_select_related = ["project", "parent"]
    list_filter = ["project", "is_demo", "created_at"]
    search_fields = ["name"]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "project":
            kwargs["queryset"] = Project.objects.filter(domain=self.domain)
        elif db_field.name == "parent":
            kwargs["queryset"] = self.model.objects.filter(project__domain=self.domain)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not change:
            _stamp_author(request, obj)
        super().save_model(request, obj, form, change)


@admin.register(ProcessingStep)
class ProcessingStepAdmin(admin.ModelAdmin):
    list_display = (
        "modality", "name", "slug", "inputs", "queue_name", "algo_name",
        "is_enabled", "is_blocking", "discard_raw",
        "prefer_processed_for_viewer", "updated_at",
    )
    list_filter = (
        "is_enabled", "is_blocking", "discard_raw",
        "prefer_processed_for_viewer", "modality",
    )
    list_editable = (
        "is_enabled", "queue_name", "algo_name", "discard_raw",
        "prefer_processed_for_viewer",
    )
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


@admin.register(SiteMaintenance)
class SiteMaintenanceAdmin(admin.ModelAdmin):
    fields = ("access_mode", "planned_message_enabled", "planned_message", "updated_at")
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
