"""Saving and reading tooth segmentation, through the same machinery measurements use.

Thin on purpose. Everything structural -- one revision spanning N images, carrying
forward the images a save did not name, the optimistic-concurrency check, the monotonic
lock -- is :func:`annotations.services.viewer.save_measurement_groups`, and this module is
what tells it that the items are tooth polygons rather than measurements.

Three differences from a measurement save, each a real one:

**Its own set kind.** ``intraoral_segmentation`` was already an ``AnnotationSet.kind`` and
already what ``annotations_convert_legacy`` writes, so reusing it is what lets
``annotations_crosscheck`` find both representations of one study. A tooth polygon is not
a measurement and filing it as one would make "this patient's measurements" mean two
different things.

**Labels are required.** An FDI code decides which segment a polygon is exported under, so
a polygon written unlabelled is a polygon that will be exported as the wrong tooth and look
fine doing it. ``apply_descriptors`` refuses rather than defaults.

**No scratch payload.** A measurement keeps one because the model stores, say, a sphere and
a radius while the viewer needs handle positions, so the items alone cannot rebuild the
viewer's state. A tooth polygon *is* a list of points; the items are the whole truth, and a
second copy allowed to go stale would only ever disagree with them.
"""

from django.core.exceptions import ValidationError

from annotations.adapters.tooth_segmentation import (
    FDI_SCHEMA_SLUG,
    FDI_SCHEMA_VERSION,
    SEGMENTATION_KIND,
    teeth_from_items,
    tooth_polygons,
)
from annotations.constants import CoordinateSystem, ResourceKind
from annotations.models import LabelSchema
from annotations.services.viewer import save_measurement_groups
from common.models import AnnotationMethod

#: The ``AnnotationMethod`` slug that gates this work. ``get_or_create_set`` asks
#: ``project_allows_annotation`` about it before writing, which is how a project with
#: tooth segmentation switched off cannot be written to through this path -- finding F11
#: is the record of what happens when a gate is a convention rather than a code path.
#:
#: The same slug ``annotations_convert_legacy`` uses for this kind, so the converted and
#: the live work are gated identically.
SEGMENTATION_METHOD_SLUG = "intraoral_segmentation"


def segmentation_method():
    """The ``AnnotationMethod`` row, or ``None`` if the registry has no such entry.

    ``None`` means "ungated", which is what ``get_or_create_set`` already does with it --
    and is the right answer for a deployment that has not registered the method rather
    than a reason to refuse every save.
    """
    return AnnotationMethod.objects.filter(slug=SEGMENTATION_METHOD_SLUG).first()


def fdi_schema():
    """The seeded FDI vocabulary.

    Refused loudly rather than created on demand: the integers are frozen by
    ``UniqueConstraint(schema, value)`` and a schema conjured at runtime would get a
    numbering nobody reviewed, under the same slug, meaning something else.
    """
    schema = LabelSchema.objects.filter(
        slug=FDI_SCHEMA_SLUG, version=FDI_SCHEMA_VERSION
    ).first()
    if schema is None:
        raise ValidationError(
            f"the {FDI_SCHEMA_SLUG} v{FDI_SCHEMA_VERSION} label schema is missing; it is "
            "seeded by annotations/migrations/0002 and must not be created at runtime"
        )
    return schema


def save_tooth_segmentation(
    patient,
    *,
    images,
    author=None,
    expected_revision=None,
    annotation_method=None,
    note="",
):
    """Write one revision holding the tooth polygons on every image named.

    :param images: ``[{"file_obj": <FileRegistry>, "teeth": {FDI: [[[x, y], ...], ...]},
        "descriptor": {...}}]``. An image with an empty ``teeth`` map is how a deletion is
        expressed -- omitting it would have the server carry the old polygons forward.
    :param expected_revision: the revision the client loaded; stale raises
        :class:`~annotations.services.exceptions.AnnotationConflict` -> 409.
    :returns: the new ``AnnotationRevision``.
    """
    groups = []
    for index, image in enumerate(images or []):
        teeth = image.get("teeth")
        if not isinstance(teeth, dict):
            raise ValidationError(f"images[{index}]: teeth must be an object keyed by FDI code")
        groups.append(
            {
                "file_obj": image.get("file_obj"),
                "file_key": None,
                # Carried so the shared writer can count and validate uniformly; the
                # translation below is what actually reads it.
                "annotations": [],
                "teeth": teeth,
                "descriptor": image.get("descriptor") or {},
                "resource_kind": ResourceKind.FILE,
                "order": index,
            }
        )

    return save_measurement_groups(
        patient,
        groups=groups,
        author=author,
        expected_revision=expected_revision,
        coordinate_system=CoordinateSystem.IMAGE_PIXEL,
        annotation_method=annotation_method or segmentation_method(),
        note=note,
        kind=SEGMENTATION_KIND,
        label_schema=fdi_schema(),
        require_labels=True,
        translate=lambda group: tooth_polygons(group["teeth"]),
        store_payload=False,
        # The first image claims the slot on the first save and keeps it thereafter.
        # `reclaim_primary=False` does not mean "no primary" -- the shared writer still
        # claims an *unset* slot, and only declines to move one somebody holds. So the
        # slot answers "what is this set mostly about" rather than "what was saved last".
        reclaim_primary=False,
        primary_index=0,
    )


def tooth_segmentation_state(patient, *, domain_field):
    """The latest polygons per file, and the revision a save must quote.

    Rebuilt from the canonical items rather than a payload -- see the module note. Only the
    latest revision is read: revisions are the audit trail and stay in the database, but
    they are not a concept the editor exposes, and falling back to an older one would
    resurrect polygons somebody deleted.

    :returns: ``{"revision": int, "setId": int|None, "images": {file_id: teeth}}``
    """
    from annotations.models import AnnotationSet, Geometry2DItem

    annotation_set = (
        AnnotationSet.objects.filter(**{domain_field: patient, "kind": SEGMENTATION_KIND})
        .order_by("id")
        .first()
    )
    if annotation_set is None:
        return {"revision": 0, "setId": None, "images": {}}

    revision = annotation_set.revisions.order_by("-revision_number").first()
    if revision is None:
        return {"revision": 0, "setId": annotation_set.id, "images": {}}

    items = (
        Geometry2DItem.objects.filter(revision=revision)
        .select_related("label", "target", "target__source_resource")
        .order_by("order", "id")
    )
    by_file = {}
    for item in items:
        file_id = item.target.source_resource.file_id if item.target else None
        if file_id is None:
            continue
        by_file.setdefault(file_id, []).append(item)

    return {
        "revision": revision.revision_number,
        "setId": annotation_set.id,
        "everAnnotated": annotation_set.ever_annotated,
        "images": {file_id: teeth_from_items(rows) for file_id, rows in by_file.items()},
    }
