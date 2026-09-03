"""The annotation model, visible and read-only.

All thirteen of these tables were invisible to staff: ``annotations`` had no
``admin.py`` at all, and in 3.0 this app holds *every* annotation in the
product. An admin could see the files an annotation was drawn on and the job
that produced them, but not the work itself -- so "why is this patient's raw
data frozen?" and "which revision wrote this?" had no answer short of a
database shell.

**Nothing here can write.** ``CONTRIBUTING.md`` makes ``annotations/services/``
the only sanctioned writer -- a view that imports an annotation model and calls
``.save()`` is a review failure -- and an editable admin would be exactly that
failure with a nicer form. The services enforce revision numbering, the
monotonic ``ever_annotated`` flag and the target fingerprints; a ``ModelAdmin``
save would enforce none of them and would leave a set whose revisions no longer
add up. Add, change and delete are all refused. Inspection is the whole value:
revisions per set, ``ever_annotated``, payload provenance, label schemas.
"""

from django.contrib import admin
from django.db.models import Count
from django.utils.html import format_html

from common.admin import ReadOnlyAdmin

from annotations.models import (
    AnnotationPayload,
    AnnotationRevision,
    AnnotationSelector,
    AnnotationSet,
    AnnotationTarget,
    EventAnnotationItem,
    Geometry2DItem,
    LabelDefinition,
    LabelSchema,
    MeasurementItem,
    SourceResource,
    SpatialAnnotation3DItem,
    TemporalAnnotationItem,
)


class ReadOnlyInline(admin.TabularInline):
    """An inline that shows rows and refuses every way of changing them."""

    extra = 0
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def _short_hash(value):
    return f"{value[:12]}…" if value and len(value) > 12 else (value or "—")


# ---------------------------------------------------------------------------
# What annotations are anchored to
# ---------------------------------------------------------------------------

@admin.register(SourceResource)
class SourceResourceAdmin(ReadOnlyAdmin):
    """The identity of the content a set is drawn on.

    ``identity_key`` is the durable name -- Cornerstone's ``imageId`` and
    friends are session identifiers and are deliberately not stored.
    """

    list_display = ["id", "kind", "identity_key", "file", "file_key", "hash", "created_at"]
    list_filter = ["kind", "created_at"]
    list_select_related = ["file"]
    search_fields = ["identity_key", "file_key", "content_hash"]
    date_hierarchy = "created_at"

    @admin.display(description="Content hash")
    def hash(self, obj):
        return _short_hash(obj.content_hash)


# ---------------------------------------------------------------------------
# The spine
# ---------------------------------------------------------------------------

class AnnotationTargetInline(ReadOnlyInline):
    model = AnnotationTarget
    fields = ["source_resource", "role", "primary_slot", "status", "order"]
    readonly_fields = fields
    verbose_name_plural = "Targets this set is anchored to"


class AnnotationRevisionInline(ReadOnlyInline):
    model = AnnotationRevision
    fields = ["revision_number", "origin", "author", "note", "created_at"]
    readonly_fields = fields
    ordering = ["-revision_number"]
    verbose_name_plural = "Revisions"


@admin.register(AnnotationSet)
class AnnotationSetAdmin(ReadOnlyAdmin):
    list_display = [
        "id", "domain", "kind", "patient_ref", "status", "ever_annotated",
        "revisions", "label_schema", "created_by", "updated_at",
    ]
    list_filter = ["domain", "kind", "status", "ever_annotated", "updated_at"]
    list_select_related = ["label_schema", "created_by", "annotation_method"]
    search_fields = ["=id", "kind", "created_by__username"]
    date_hierarchy = "updated_at"
    inlines = [AnnotationTargetInline, AnnotationRevisionInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_revisions=Count("revisions"))

    @admin.display(description="Patient")
    def patient_ref(self, obj):
        """The patient, without asking which of three FK columns holds it.

        Reads the stored ids rather than calling ``get_patient()``: the
        changelist would otherwise be one extra query per row for a label that
        is just a number.
        """
        pk = obj.patient_id or obj.brain_patient_id or obj.laparoscopy_patient_id
        return f"{obj.domain}#{pk}" if pk else "—"

    @admin.display(description="Revisions", ordering="_revisions")
    def revisions(self, obj):
        return getattr(obj, "_revisions", 0)


@admin.register(AnnotationTarget)
class AnnotationTargetAdmin(ReadOnlyAdmin):
    list_display = [
        "id", "annotation_set", "source_resource", "role", "primary_slot",
        "status", "order", "created_at",
    ]
    list_filter = ["role", "status", "created_at"]
    list_select_related = ["annotation_set", "source_resource"]
    search_fields = ["=id", "role", "source_resource__identity_key"]


@admin.register(AnnotationSelector)
class AnnotationSelectorAdmin(ReadOnlyAdmin):
    """Which part of a target the work applies to -- frame, slice or interval."""

    list_display = [
        "id", "target", "kind", "coordinate_system", "frame_index",
        "slice_axis", "slice_index", "start_time_ms", "end_time_ms",
        "segment_value",
    ]
    list_filter = ["kind", "coordinate_system", "slice_axis"]
    list_select_related = ["target"]
    search_fields = ["=id", "=target__id"]


@admin.register(AnnotationRevision)
class AnnotationRevisionAdmin(ReadOnlyAdmin):
    """Who wrote a version of a set, and what the targets looked like then.

    ``source_fingerprint`` is the record of the content the revision was drawn
    on; a later mismatch is what makes a silently-swapped input detectable.
    """

    list_display = [
        "id", "annotation_set", "revision_number", "origin", "author",
        "fingerprinted", "note", "created_at",
    ]
    list_filter = ["origin", "created_at"]
    list_select_related = ["annotation_set", "author"]
    search_fields = ["=id", "note", "author__username"]
    date_hierarchy = "created_at"

    @admin.display(description="Fingerprinted", boolean=True)
    def fingerprinted(self, obj):
        return bool(obj.source_fingerprint)


@admin.register(AnnotationPayload)
class AnnotationPayloadAdmin(ReadOnlyAdmin):
    """Dense artifacts: the labelmaps and blobs a revision points at."""

    list_display = [
        "id", "revision", "format", "canonical_slot", "variant", "file",
        "hash", "byte_size", "created_at",
    ]
    list_filter = ["format", "variant", "created_at"]
    list_select_related = ["revision", "file"]
    search_fields = ["=id", "content_hash", "variant"]
    date_hierarchy = "created_at"

    @admin.display(description="Content hash")
    def hash(self, obj):
        return _short_hash(obj.content_hash)


# ---------------------------------------------------------------------------
# What an annotation means
# ---------------------------------------------------------------------------

class LabelDefinitionInline(ReadOnlyInline):
    model = LabelDefinition
    fields = ["value", "code", "display_name", "color", "order", "is_active"]
    readonly_fields = fields
    ordering = ["order", "value"]
    verbose_name_plural = "Labels in this schema"


@admin.register(LabelSchema)
class LabelSchemaAdmin(ReadOnlyAdmin):
    list_display = ["id", "name", "slug", "version", "domain", "labels", "is_active", "created_at"]
    list_filter = ["domain", "is_active", "version"]
    list_select_related = ["created_by"]
    search_fields = ["name", "slug", "description"]
    inlines = [LabelDefinitionInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_labels=Count("definitions"))

    @admin.display(description="Labels", ordering="_labels")
    def labels(self, obj):
        return getattr(obj, "_labels", 0)


@admin.register(LabelDefinition)
class LabelDefinitionAdmin(ReadOnlyAdmin):
    list_display = ["id", "schema", "value", "code", "display_name", "swatch", "order", "is_active"]
    list_filter = ["schema", "is_active"]
    list_select_related = ["schema"]
    search_fields = ["code", "display_name", "schema__name"]

    @admin.display(description="Color")
    def swatch(self, obj):
        return format_html(
            '<span style="display:inline-block;width:14px;height:14px;'
            'border-radius:50%;background:{};border:1px solid #ccc;"></span>',
            obj.color or "transparent",
        )


# ---------------------------------------------------------------------------
# The items themselves
# ---------------------------------------------------------------------------

class AnnotationItemAdmin(ReadOnlyAdmin):
    """Shared changelist for the five item tables.

    Four columns are on ``AnnotationItemBase`` and mean the same thing in every
    subclass; each admin below adds only what its own table carries.
    """

    base_display = ["id", "revision", "target", "selector", "label", "order"]
    list_select_related = ["revision", "target", "selector", "label"]
    search_fields = ["=id", "=revision__id"]


@admin.register(Geometry2DItem)
class Geometry2DItemAdmin(AnnotationItemAdmin):
    list_display = AnnotationItemAdmin.base_display + [
        "geometry_type", "coordinate_system", "closed", "stroke_width",
    ]
    list_filter = ["geometry_type", "coordinate_system", "closed"]


@admin.register(SpatialAnnotation3DItem)
class SpatialAnnotation3DItemAdmin(AnnotationItemAdmin):
    list_display = AnnotationItemAdmin.base_display + [
        "geometry_type", "coordinate_system",
    ]
    list_filter = ["geometry_type", "coordinate_system"]


@admin.register(MeasurementItem)
class MeasurementItemAdmin(AnnotationItemAdmin):
    list_display = AnnotationItemAdmin.base_display + [
        "kind", "value", "unit", "is_calibrated", "sample_count",
    ]
    list_filter = ["kind", "unit", "is_calibrated"]


@admin.register(TemporalAnnotationItem)
class TemporalAnnotationItemAdmin(AnnotationItemAdmin):
    list_display = AnnotationItemAdmin.base_display + ["start_time_ms", "end_time_ms"]


@admin.register(EventAnnotationItem)
class EventAnnotationItemAdmin(AnnotationItemAdmin):
    list_display = AnnotationItemAdmin.base_display + ["event_type", "time_ms", "value"]
    list_filter = ["event_type"]
