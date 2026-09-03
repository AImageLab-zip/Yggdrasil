"""Everything the admin knows about ``common``'s own models, in one place.

Six of these registrations used to live in ``maxillo/admin.py`` -- ``Modality``,
``AnnotationMethod``, ``ProjectAccess``, ``Job``, ``FileRegistry`` and
``Invitation``. They rendered under the *Common* heading (Django files a model
by the app that declares it, not by the file that registers it), so the split
was invisible in the UI and load-bearing in the code: dropping ``maxillo`` from
``INSTALLED_APPS`` would have silently taken half of ``common``'s admin with it,
and a reader looking for the Job admin had no reason to look in a domain app.
They are here now. ``maxillo/admin.py`` registers maxillo models and nothing
else, and the same is true of ``brain`` and ``laparoscopy``.

The index itself is grouped by purpose rather than by app label; see
:mod:`common.admin_site`, which is installed at the bottom of this module.
"""

import functools
from collections import defaultdict

from django import forms
from django.apps import apps
from django.contrib import admin, messages
from django.contrib.admin import helpers as admin_helpers
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.html import format_html

from common.admin_site import install as _install_admin_site
from common.annotation_lock import (
    annotation_lock_reasons,
    is_raw_file_type,
    lock_message,
    raw_data_is_locked,
)
from common.deletion import deletion_summary, delete_project
from common.domains import fk_fields_for
from common.models import (
    ActivityEvent,
    AnnotationMethod,
    FileRegistry,
    Invitation,
    Job,
    Modality,
    Notification,
    ProcessingStep,
    Project,
    ProjectAccess,
    RecentlyViewed,
    SiteMaintenance,
    SystemCheck,
    UserPreference,
    UserSession,
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


# ---------------------------------------------------------------------------
# Confirmation pages for actions that do something a click cannot be taken back
# ---------------------------------------------------------------------------

def confirm_action(
    modeladmin,
    request,
    queryset,
    *,
    action_name,
    title,
    lead,
    confirm_label,
    consequence="",
    lines=(),
):
    """Render the "are you sure" page for a destructive/dispatching action.

    Django's action framework re-runs the action when the returned form posts
    back with the same ``action`` and ``_selected_action`` values, so an action
    that wants a confirmation step returns this on the first pass and does its
    work on the second (``request.POST["confirmed"] == "yes"``). This is the
    same shape as the built-in "delete selected" flow.
    """
    lines = list(lines)
    context = {
        **modeladmin.admin_site.each_context(request),
        "title": title,
        "lead": lead,
        "consequence": consequence,
        "object_lines": lines[:20],
        "more_count": max(len(lines) - 20, 0),
        "queryset": queryset,
        "action_name": action_name,
        "action_checkbox_name": admin_helpers.ACTION_CHECKBOX_NAME,
        "confirm_label": confirm_label,
        "opts": modeladmin.model._meta,
        "media": modeladmin.media,
    }
    return TemplateResponse(
        request, "admin/common/action_confirmation.html", context
    )


def _is_confirmed(request):
    return request.POST.get("confirmed") == "yes"


# ---------------------------------------------------------------------------
# Bulk evaluation of the raw-data lock
# ---------------------------------------------------------------------------

def raw_lock_map(patients):
    """``{pk: bool}`` -- :func:`~common.annotation_lock.raw_data_is_locked` for
    many patients of **one** domain, in a query count that does not grow with
    the number of patients.

    ``raw_locked`` is a changelist column on ``FileRegistry`` (37k rows) and on
    ``laparoscopy.Patient``, and the per-object predicate costs up to six
    queries: a page of 100 rows was 600. The predicate in
    :mod:`common.annotation_lock` stays the authority on *what* locks a patient
    -- this is the same union, asked once per table instead of once per row, and
    ``tests_admin_queries`` asserts the two agree row by row.
    """
    patients = [p for p in patients if p is not None]
    if not patients:
        return {}

    from annotations.models import AnnotationSet

    domain = patients[0]._meta.app_label
    patient_fk, _voice_fk = fk_fields_for(domain)
    pks = {p.pk for p in patients}

    # 1. The annotations half. `ever_annotated` anywhere locks the patient
    #    (the raw lock asks with include_panoramic=True, so no kind is exempt);
    #    the full set of kinds present is what tells the legacy half below which
    #    tables the conversion has already covered and must not be asked about.
    kinds = defaultdict(dict)
    rows = AnnotationSet.objects.filter(**{f"{patient_fk}__in": pks}).values_list(
        patient_fk, "kind", "ever_annotated"
    )
    for pid, kind, ever in rows:
        kinds[pid][kind] = kinds[pid].get(kind, False) or bool(ever)

    locked = {p.pk: any(kinds[p.pk].values()) for p in patients}
    pending = {pk for pk, is_locked in locked.items() if not is_locked}
    if not pending:
        return locked

    def mark(kind, pk_iterable):
        """Lock every still-open patient in ``pk_iterable`` the conversion has
        not already answered for ``kind``."""
        for pk in pk_iterable:
            if pk in pending and kind not in kinds[pk]:
                locked[pk] = True

    def owners(queryset):
        return set(queryset.filter(patient__in=pending).values_list("patient", flat=True))

    model = functools.partial(apps.get_model, domain)

    # Voice captions exist in all three domains.
    mark("voice_caption", owners(model("VoiceCaption").objects.all()))

    if domain == "maxillo":
        human = model("Classification").objects.exclude(classifier="pipeline")
        mark("occlusion_classification", owners(human))
        mark(
            "intraoral_segmentation",
            owners(model("IntraoralToothSegmentation").objects.all()),
        )
        mark(
            "ios_landmarks",
            owners(FileRegistry.objects.filter(file_type="ios_landmarks")),
        )
        mark(
            "panoramic_arch",
            owners(model("PanoramicState").objects.filter(geometry_source="custom_cp")),
        )
    elif domain == "laparoscopy":
        human = model("Classification").objects.exclude(classifier="pipeline")
        mark("study_notes", owners(human))
        mark(
            "video_quadrants",
            owners(model("QuadrantClassificationMarker").objects.all()),
        )
        mark("video_regions", owners(model("RegionAnnotation").objects.all()))
    # brain has no annotation tables of its own; voice captions are it.

    return locked


def stamp_raw_locks(rows, patient_of):
    """Attach ``_raw_locked`` to every row in ``rows``, in bulk.

    ``patient_of(row)`` returns the patient a row's lock is about (itself, for a
    patient changelist). Rows are grouped by domain because ``raw_lock_map``
    answers one domain at a time.
    """
    by_domain = defaultdict(list)
    for row in rows:
        patient = patient_of(row)
        if patient is not None:
            by_domain[patient._meta.app_label].append((row, patient))
    for pairs in by_domain.values():
        locked = raw_lock_map([patient for _row, patient in pairs])
        for row, patient in pairs:
            row._raw_locked = locked.get(patient.pk, False)


def raw_locked_changelist(patient_of):
    """A ``ChangeList`` that resolves the raw lock for the whole page at once.

    ``get_results`` leaves ``result_list`` as an unevaluated queryset; the page
    is fetched here instead of in the template, which costs the same one query
    and gives the rows somewhere to be stamped.
    """
    from django.contrib.admin.views.main import ChangeList

    class _RawLockChangeList(ChangeList):
        def get_results(self, request):
            super().get_results(request)
            rows = list(self.result_list)
            stamp_raw_locks(rows, patient_of)
            self.result_list = rows

    return _RawLockChangeList


# ---------------------------------------------------------------------------
# Projects & access
# ---------------------------------------------------------------------------

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
    list_select_related = ['created_by']
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


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    """Every project, including the ones no domain admin can show.

    The three proxies each filter to their own ``domain``, so a project whose
    domain slug is misspelt or was written before the column existed appears in
    none of them -- invisible in an admin that has four project changelists.
    This is the unfiltered inventory, kept deliberately small: repairing
    ``domain`` is what it is for. Creating and deleting stay with the domain
    admins, which force the domain and route the delete through
    :mod:`common.deletion`.
    """

    list_display = ["name", "slug", "domain", "is_active", "created_at", "created_by"]
    list_filter = ["domain", "is_active"]
    list_select_related = ["created_by"]
    search_fields = ["name", "slug", "description"]
    ordering = ["domain", "name"]
    fields = ["name", "slug", "domain", "description", "icon", "is_active"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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


@admin.register(ProjectAccess)
class ProjectAccessAdmin(admin.ModelAdmin):
    list_display = ['user', 'project', 'role', 'created_at']
    list_filter = ['role', 'created_at', 'project__domain']
    list_select_related = ['user', 'project']
    search_fields = ['user__username', 'project__name']
    autocomplete_fields = ['user', 'project']


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ['code', 'email', 'role', 'project_list', 'created_by', 'created_at', 'expires_at', 'used_at', 'used_by']
    list_filter = ['role', 'created_at', 'expires_at']
    list_select_related = ['created_by', 'used_by']
    search_fields = ['code', 'email', 'created_by__username', 'used_by__username']
    readonly_fields = ['code', 'created_at', 'used_at', 'used_by']
    autocomplete_fields = ['created_by', 'used_by', 'projects']

    def get_queryset(self, request):
        # `project_list` reads the M2M for every row; without this it is one
        # query per invitation.
        return super().get_queryset(request).prefetch_related('projects')

    def project_list(self, obj):
        return ', '.join(project.name for project in obj.projects.all())
    project_list.short_description = 'Projects'

    def get_readonly_fields(self, request, obj=None):
        if obj:  # Editing existing object
            return self.readonly_fields + ['created_by']
        return self.readonly_fields


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

    **``is_demo`` is not an inline checkbox.** It was in ``list_editable``, one
    click and a Save away, and what it does is publish the folder's patients to
    the *anonymous* public demo (``common.demo``). A grid checkbox is the wrong
    shape for that: nothing on the row says what it means and nothing asks. It
    is a pair of named actions with a confirmation page instead, still usable in
    bulk, and it remains editable on the change form where the field's help text
    is visible.
    """

    #: The domain slug this admin serves. Subclasses must set it.
    domain = None

    list_display = ["name", "project", "parent", "is_demo", "created_at", "created_by"]
    list_select_related = ["project", "parent", "created_by"]
    list_filter = ["project", "is_demo", "created_at"]
    search_fields = ["name"]
    actions = ["publish_to_demo", "withdraw_from_demo"]

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

    @admin.action(description="Publish to the public demo")
    def publish_to_demo(self, request, queryset):
        queryset = queryset.filter(is_demo=False)
        if not queryset.exists():
            self.message_user(
                request, "Every selected folder is already in the demo.", messages.INFO
            )
            return None
        if not _is_confirmed(request):
            return confirm_action(
                self, request, queryset,
                action_name="publish_to_demo",
                title="Publish folders to the public demo?",
                consequence=(
                    "Anyone on the internet, signed in or not, will be able to "
                    "read the patients in these folders."
                ),
                lead=f"{queryset.count()} folder(s) will become publicly readable:",
                lines=[str(folder) for folder in queryset],
                confirm_label="Yes, publish them",
            )
        count = queryset.update(is_demo=True)
        self.message_user(request, f"Published {count} folder(s) to the public demo.")

    @admin.action(description="Remove from the public demo")
    def withdraw_from_demo(self, request, queryset):
        count = queryset.filter(is_demo=True).update(is_demo=False)
        self.message_user(request, f"Removed {count} folder(s) from the public demo.")


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

class ProcessingStepInline(admin.TabularInline):
    model = ProcessingStep
    can_delete = True
    filter_horizontal = ['depends_on']
    readonly_fields = ['updated_at']
    prepopulated_fields = {"slug": ("name",)}
    extra = 0


@admin.register(Modality)
class ModalityAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'domain', 'label', 'icon', 'is_active', 'created_at', 'created_by']
    list_filter = ['domain', 'is_active', 'created_at']
    list_select_related = ['created_by']
    search_fields = ['name', 'description', 'slug', 'label', 'icon']
    prepopulated_fields = {"slug": ("name",)}
    inlines = [ProcessingStepInline]


@admin.register(AnnotationMethod)
class AnnotationMethodAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'domain', 'is_active', 'created_at']
    list_filter = ['domain', 'is_active']
    search_fields = ['name', 'slug', 'description']
    prepopulated_fields = {"slug": ("name",)}


@admin.register(ProcessingStep)
class ProcessingStepAdmin(admin.ModelAdmin):
    """The pipeline definition.

    ``algo_name`` and ``discard_raw`` were in ``list_editable``. Neither belongs
    in a grid of text inputs saved by one button: ``algo_name`` resolves to
    ``ALGO_BASE_DIR/<algo_name>/run.sbatch`` on the cluster, so a typo silently
    points a step at a script that does not exist, and ``discard_raw`` is the
    switch that destroys raw inputs once a step succeeds. They are still
    editable -- on the change form, one step at a time, next to their help text.
    """

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
        "is_enabled", "queue_name", "prefer_processed_for_viewer",
    )
    list_select_related = ("modality",)
    search_fields = ("name", "slug", "modality__name", "modality__slug", "queue_name")
    autocomplete_fields = ("modality",)
    filter_horizontal = ("depends_on",)
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("updated_at",)

    def get_queryset(self, request):
        # `inputs` renders every dependency of every row.
        return super().get_queryset(request).prefetch_related("depends_on")

    @admin.display(description="Inputs")
    def inputs(self, obj):
        return ", ".join(s.slug for s in obj.depends_on.all()) or "—"


#: Written into ``Job.error_logs`` by :meth:`JobAdmin.cancel_pending_jobs`.
#: ``Job.STATUS_CHOICES`` has no ``cancelled`` member, so a cancelled job is
#: stored as ``failed`` and is otherwise indistinguishable from one the cluster
#: actually failed -- ``can_retry()`` and ``retry_count`` then treat it as a
#: failure. Adding the status is the real fix and it belongs in
#: ``common/models.py`` plus a migration; until then this marker is what makes
#: the two tellable apart, in the record and on the changelist.
JOB_CANCELLED_MARKER = "[cancelled-in-admin]"

#: Most jobs one click may re-dispatch to the cluster. Saving a Job with status
#: ``retrying`` fires ``common.signals._job_post_save``, which sends a Celery
#: task per row; a 500-row selection was 500 SLURM submissions.
JOB_RETRY_CAP = 25


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ['id', 'modality_slug', 'step', 'status', 'cancelled', 'patient', 'voice_caption', 'priority', 'dependencies_count', 'created_at', 'started_at', 'completed_at', 'retry_count']
    list_filter = ['modality_slug', 'status', 'created_at', 'started_at', 'completed_at', 'priority', ('dependencies', admin.EmptyFieldListFilter)]
    list_select_related = ['step', 'patient', 'voice_caption']
    search_fields = ['patient__patient_id', 'voice_caption__id', 'worker_id']
    autocomplete_fields = ['dependencies', 'step', 'patient', 'voice_caption']
    readonly_fields = ['created_at', 'started_at', 'completed_at', 'dependencies_list']

    fieldsets = (
        ('Job Information', {
            'fields': ('modality_slug', 'step', 'status', 'priority', 'patient', 'voice_caption')
        }),
        ('Dependencies', {
            'fields': ('dependencies', 'dependencies_list'),
            'description': 'Jobs that must complete before this job can start'
        }),
        ('Files & Processing', {
            'fields': ('input_files', 'output_files')
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
            return self.readonly_fields + ['modality_slug', 'patient', 'voice_caption', 'input_files']
        return self.readonly_fields

    @admin.display(description="Cancelled", boolean=True)
    def cancelled(self, obj):
        """True for a job an admin cancelled rather than one that failed."""
        return JOB_CANCELLED_MARKER in (obj.error_logs or "")

    def dependencies_count(self, obj):
        """Display the number of dependencies for this job"""
        count = getattr(obj, 'dependencies_count_annotated', None)
        if count is None:
            count = obj.dependencies.count()
        if count == 0:
            return "-"
        return f"{count} dep(s)"
    dependencies_count.short_description = "Dependencies"

    def dependencies_list(self, obj):
        """Display a list of dependency job IDs"""
        deps = list(obj.dependencies.all()[:4])
        if not deps:
            return "-"
        dep_ids = [f"#{dep.id}" for dep in deps[:3]]
        if len(deps) > 3:
            dep_ids.append("… (more)")
        return ", ".join(dep_ids)
    dependencies_list.short_description = "Dependency Jobs"

    def get_queryset(self, request):
        """Optimize queryset for changelist dependency rendering."""
        return super().get_queryset(request).annotate(
            dependencies_count_annotated=Count('dependencies', distinct=True)
        )

    def get_fieldsets(self, request, obj=None):
        """Customize fieldsets based on job status"""
        fieldsets = list(super().get_fieldsets(request, obj))

        # Add dependent jobs info if this job has dependents
        dependent_count = 0
        if obj:
            dependent_count = obj.dependent_jobs.count()
        if dependent_count:
            dependent_info = {
                'fields': (),
                'description': f'This job has {dependent_count} dependent job(s) waiting for it to complete'
            }
            fieldsets.append(('Dependent Jobs', dependent_info))

        return fieldsets

    actions = ['retry_failed_jobs', 'cancel_pending_jobs', 'check_dependencies', 'clear_dependencies']

    @admin.action(description="Retry selected failed jobs")
    def retry_failed_jobs(self, request, queryset):
        eligible = [job for job in queryset.filter(status='failed') if job.can_retry()]
        if not eligible:
            self.message_user(
                request, "No selected job is a failed job that can be retried.",
                messages.INFO,
            )
            return None
        if len(eligible) > JOB_RETRY_CAP:
            self.message_user(
                request,
                f"{len(eligible)} jobs would be re-dispatched to the cluster; the "
                f"limit for one action is {JOB_RETRY_CAP}. Narrow the selection.",
                messages.ERROR,
            )
            return None
        if not _is_confirmed(request):
            return confirm_action(
                self, request, queryset,
                action_name="retry_failed_jobs",
                title="Retry failed jobs?",
                consequence=(
                    f"This dispatches {len(eligible)} job(s) to the compute "
                    "cluster immediately, one SLURM submission each."
                ),
                lead="These jobs will be re-queued:",
                lines=[f"#{job.id} {job.modality_slug} ({job.status})" for job in eligible],
                confirm_label=f"Yes, retry {len(eligible)} job(s)",
            )
        for job in eligible:
            job.status = 'retrying'
            job.save()
        self.message_user(request, f'Retried {len(eligible)} failed job(s).')

    @admin.action(description="Cancel selected pending jobs")
    def cancel_pending_jobs(self, request, queryset):
        pending = queryset.filter(status__in=['pending', 'retrying'])
        count = pending.count()
        if not count:
            self.message_user(
                request, "No selected job is pending or retrying.", messages.INFO
            )
            return None
        if not _is_confirmed(request):
            return confirm_action(
                self, request, queryset,
                action_name="cancel_pending_jobs",
                title="Cancel pending jobs?",
                consequence=(
                    "Job has no 'cancelled' status, so these are stored as "
                    "failed. They are stamped as cancelled in the error log and "
                    "flagged in the Cancelled column, but any report counting "
                    "failures will count them."
                ),
                lead=f"{count} pending job(s) will be stopped:",
                lines=[f"#{job.id} {job.modality_slug} ({job.status})" for job in pending],
                confirm_label=f"Yes, cancel {count} job(s)",
            )
        # `.update()` deliberately: a per-row save would re-fire
        # `_job_post_save` and re-dispatch exactly the jobs being stopped.
        stamped = pending.update(
            status='failed',
            completed_at=timezone.now(),
            error_logs=(
                f"{JOB_CANCELLED_MARKER} cancelled by {request.user} "
                f"on {timezone.now():%Y-%m-%d %H:%M} — not a processing failure."
            ),
        )
        self.message_user(
            request,
            f"Cancelled {stamped} pending job(s). They are recorded as failed "
            f"and marked {JOB_CANCELLED_MARKER}.",
            messages.WARNING,
        )

    @admin.action(description="Check and update dependency status")
    def check_dependencies(self, request, queryset):
        if not _is_confirmed(request):
            return confirm_action(
                self, request, queryset,
                action_name="check_dependencies",
                title="Re-check dependency status?",
                consequence=(
                    "A job whose dependencies are now satisfied becomes "
                    "'pending' and is dispatched to the cluster at once."
                ),
                lead=f"{queryset.count()} job(s) will be re-evaluated.",
                confirm_label="Yes, re-check them",
            )
        count = 0
        for job in queryset:
            if job.update_status_based_on_dependencies():
                count += 1
        self.message_user(request, f'Updated dependency status for {count} job(s).')

    @admin.action(description="Clear all dependencies")
    def clear_dependencies(self, request, queryset):
        with_deps = [job for job in queryset if job.dependencies.exists()]
        if not with_deps:
            self.message_user(
                request, "None of the selected jobs has dependencies.", messages.INFO
            )
            return None
        if not _is_confirmed(request):
            return confirm_action(
                self, request, with_deps,
                action_name="clear_dependencies",
                title="Clear job dependencies?",
                consequence=(
                    "The dependency edges are destroyed and cannot be "
                    "reconstructed. A job left with none is then dispatched to "
                    "the cluster whether or not its inputs exist."
                ),
                lead=f"{len(with_deps)} job(s) will lose every dependency:",
                lines=[
                    f"#{job.id} {job.modality_slug} — {job.dependencies.count()} dep(s)"
                    for job in with_deps
                ],
                confirm_label=f"Yes, clear {len(with_deps)} job(s)",
            )
        for job in with_deps:
            job.dependencies.clear()
            job.update_status_based_on_dependencies()
        self.message_user(request, f'Cleared dependencies for {len(with_deps)} job(s).')


# ---------------------------------------------------------------------------
# Imaging catalog
# ---------------------------------------------------------------------------

class FileRegistryAdminForm(forms.ModelForm):
    """Refuses to register a new raw file for an already-annotated patient.

    The change form is closed off by ``has_change_permission``, but adding a
    *new* raw row pointing at different bytes is a re-upload by another name, so
    it needs its own gate. ``lock_bypass`` is set per-request by
    ``FileRegistryAdmin.get_form``.
    """

    lock_bypass = False

    class Meta:
        model = FileRegistry
        fields = '__all__'

    def clean(self):
        cleaned = super().clean()
        if self.lock_bypass:
            return cleaned
        # Only the maxillo `patient` FK is exposed by the fieldsets below, so an
        # added row can only ever belong to a maxillo patient.
        patient = cleaned.get('patient')
        if patient is None or not is_raw_file_type(cleaned.get('file_type')):
            return cleaned
        reasons = annotation_lock_reasons(patient)
        if reasons:
            raise ValidationError(lock_message(reasons))
        return cleaned


@admin.register(FileRegistry)
class FileRegistryAdmin(admin.ModelAdmin):
    form = FileRegistryAdminForm
    list_display = ['id', 'file_type', 'patient', 'voice_caption', 'raw_locked', 'file_size_mb', 'created_at', 'modality']
    list_filter = ['file_type', 'domain', 'created_at']
    list_select_related = ['patient', 'voice_caption', 'modality']
    search_fields = ['file_path', 'patient__patient_id', 'voice_caption__id']
    readonly_fields = ['created_at', 'file_hash', 'file_size', 'file_size_mb']
    # 37k rows: every one of these pickers was a full <select> of a growing table.
    autocomplete_fields = ['patient', 'voice_caption', 'processing_job', 'modality']

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

    def get_changelist(self, request, **kwargs):
        # `raw_locked` costs up to six queries per row when asked one row at a
        # time. This resolves the whole page in a fixed number instead.
        return raw_locked_changelist(lambda row: row.get_patient())

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

    def _is_locked_raw(self, request, obj):
        """Whether this row is a raw input frozen by existing annotation work.

        Superusers are exempt: admin is the one place a genuine data-repair still
        has to be possible. Everything else the app does is unconditional.
        """
        if obj is None or request.user.is_superuser:
            return False
        if not is_raw_file_type(obj.file_type):
            return False
        return raw_data_is_locked(obj.get_patient())

    def has_change_permission(self, request, obj=None):
        # Returning False makes Django render the read-only view form and drop
        # the save row, rather than us having to enumerate every field.
        if self._is_locked_raw(request, obj):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        # `django.contrib.admin.utils.get_deleted_objects` consults this per
        # object, so this covers the bulk "delete selected" action too.
        if self._is_locked_raw(request, obj):
            return False
        return super().has_delete_permission(request, obj)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # modelform_factory hands back a fresh subclass per call, so stamping the
        # per-request bypass onto it cannot leak into another request.
        form.lock_bypass = request.user.is_superuser
        return form

    @admin.display(description='Raw locked', boolean=True)
    def raw_locked(self, obj):
        """Visible on the changelist so the freeze is obvious before opening a row."""
        if not is_raw_file_type(obj.file_type):
            return False
        cached = getattr(obj, '_raw_locked', None)
        if cached is not None:
            return cached
        return raw_data_is_locked(obj.get_patient())


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

class ReadOnlyAdmin(admin.ModelAdmin):
    """A changelist for rows only the application writes.

    Unlike the ``ReadOnlyAdminMixin`` this replaces -- three overrides that each
    returned ``super()``, i.e. nothing, on thirteen admins that were fully
    editable -- these really do refuse. View permission is untouched: seeing the
    rows is the entire point.
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SystemCheck)
class SystemCheckAdmin(ReadOnlyAdmin):
    list_display = ("name", "status", "ran_at", "duration_ms")
    list_filter = ("name", "status")
    date_hierarchy = "ran_at"


@admin.register(SiteMaintenance)
class SiteMaintenanceAdmin(admin.ModelAdmin):
    fields = ("access_mode", "planned_message_enabled", "planned_message", "updated_at")
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UserSession)
class UserSessionAdmin(ReadOnlyAdmin):
    """Reconstructed activity windows, written by ``common.presence``."""

    list_display = ("user", "project_slug", "started_at", "last_seen_at", "minutes")
    list_filter = ("project_slug", "started_at")
    list_select_related = ("user",)
    search_fields = ("user__username", "project_slug")
    date_hierarchy = "started_at"

    @admin.display(description="Minutes")
    def minutes(self, obj):
        return f"{obj.duration_seconds / 60:.1f}"


@admin.register(ActivityEvent)
class ActivityEventAdmin(ReadOnlyAdmin):
    """The cross-domain audit feed, written by ``common.activity.log_activity``."""

    list_display = ("created_at", "actor", "verb", "domain", "patient_name", "target")
    list_filter = ("domain", "verb", "created_at")
    list_select_related = ("actor",)
    search_fields = ("verb", "target", "patient_name", "actor__username")
    date_hierarchy = "created_at"


@admin.register(Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "user", "level", "message", "is_read")
    list_filter = ("level", "is_read", "created_at")
    list_select_related = ("user",)
    search_fields = ("message", "user__username")


@admin.register(RecentlyViewed)
class RecentlyViewedAdmin(ReadOnlyAdmin):
    list_display = ("user", "domain", "patient_pk", "patient_name", "project_label", "viewed_at")
    list_filter = ("domain", "viewed_at")
    list_select_related = ("user",)
    search_fields = ("user__username", "patient_name")


@admin.register(UserPreference)
class UserPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "report_language", "updated_at")
    list_filter = ("report_language",)
    list_select_related = ("user",)
    search_fields = ("user__username",)
    autocomplete_fields = ("user",)


# The index is grouped by purpose rather than by app label. Installed here
# rather than through ``settings.INSTALLED_APPS`` so that no shared settings
# change is needed; ``common.admin_site.install`` explains what it does and what
# the settings version of it would look like.
_install_admin_site(admin.site)
