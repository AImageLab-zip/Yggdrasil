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

from annotations.adapters.image_edit_replay import transform_teeth
from annotations.adapters.tooth_segmentation import (
    SEGMENTATION_KIND,
    teeth_from_items,
    tooth_polygons,
)
from annotations.constants import (
    AnnotationOrigin,
    AnnotationStatus,
    CoordinateSystem,
    ResourceKind,
)
from annotations.services.exceptions import AnnotationConflict
# Re-exported: this was `fdi_schema`'s home until IOS landmarks became a second caller,
# and the import path is part of this module's surface.
from annotations.services.labels import fdi_schema
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


#: The target role this surface anchors under.
#:
#: Byte-identical to what ``annotations_convert_legacy`` passes, and that is the whole
#: point. ``attach_target`` keys on ``(annotation_set, source_resource, role)``, so a live
#: save under a different role would create a *second* target for the same photograph --
#: and because ``tooth_segmentation_state`` groups items by file id, the converted
#: polygons and the freshly drawn ones would both come back, doubled, on every study
#: anybody had edited.
IMAGE_ROLE = "image"


def save_tooth_segmentation(
    patient,
    *,
    images,
    author=None,
    expected_revision=None,
    annotation_method=None,
    note="",
    origin=AnnotationOrigin.MANUAL,
):
    """Write one revision holding the tooth polygons on every image named.

    :param images: ``[{"file_obj": <FileRegistry>, "teeth": {FDI: [[[x, y], ...], ...]},
        "descriptor": {...}, "confirmed": True|False|None}]``. An image with an empty
        ``teeth`` map is how a deletion is expressed -- omitting it would have the server
        carry the old polygons forward. ``confirmed`` absent or ``None`` leaves the
        image's confirmation exactly as it was; a save that never mentions it cannot
        clear it.
    :param expected_revision: the revision the client loaded; stale raises
        :class:`~annotations.services.exceptions.AnnotationConflict` -> 409.
    :param origin: ``PREDICTION`` for the segmentation job's output, which is what keeps
        model output from setting the monotonic ``ever_annotated`` flag.
    :returns: the new ``AnnotationRevision``.
    :raises AnnotationConflict: on an attempt to change a confirmed image's polygons
        without reopening it in the same call -- the legacy editor's 409, kept.
    """
    images = list(images or [])
    current = tooth_segmentation_state(patient, domain_field=_domain_field(patient))

    groups = []
    for index, image in enumerate(images):
        teeth = image.get("teeth")
        if not isinstance(teeth, dict):
            raise ValidationError(f"images[{index}]: teeth must be an object keyed by FDI code")
        file_obj = image.get("file_obj")
        confirmed = image.get("confirmed")

        # The confirmation gate, before anything is written. A confirmed image is a claim
        # somebody signed; changing its polygons silently would retract that claim while
        # leaving it displayed. Reopening in the same call is allowed, which is exactly
        # what the "Reopen before editing" message tells the user to do.
        file_id = getattr(file_obj, "pk", None)
        was_confirmed = current["confirmations"].get(file_id) is True
        if was_confirmed and confirmed is not False:
            if _normalized(teeth) != _normalized(current["images"].get(file_id, {})):
                raise AnnotationConflict(
                    "Segmentation is confirmed. Reopen before editing."
                )

        group = {
            "file_obj": file_obj,
            "file_key": None,
            # Carried so the shared writer can count and validate uniformly; the
            # translation below is what actually reads it.
            "annotations": [],
            "teeth": teeth,
            "descriptor": image.get("descriptor") or {},
            "resource_kind": ResourceKind.FILE,
            "role": IMAGE_ROLE,
            "order": index,
        }
        if confirmed is not None:
            group["status"] = (
                AnnotationStatus.CONFIRMED if confirmed else AnnotationStatus.DRAFT
            )
        groups.append(group)

    return save_measurement_groups(
        patient,
        groups=groups,
        author=author,
        expected_revision=expected_revision,
        coordinate_system=CoordinateSystem.IMAGE_PIXEL,
        annotation_method=annotation_method or segmentation_method(),
        note=note,
        origin=origin,
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


#: ``AnnotationSet``'s patient FK for each domain. Tooth segmentation is maxillo-only
#: today, but resolving it from the model rather than hardcoding ``patient`` is what keeps
#: this from being the line that breaks when a second domain grows photographs.
_DOMAIN_FIELDS = {"maxillo": "patient", "brain": "brain_patient"}


def _domain_field(patient):
    return _DOMAIN_FIELDS.get(patient._meta.app_label, "laparoscopy_patient")


def _normalized(teeth):
    """One comparable form of a teeth map, for the confirmation gate only.

    Floats to floats and tuples to lists, so ``[[1, 2]]`` and ``[[1.0, 2.0]]`` compare
    equal. Without it a client that round-tripped its own polygons through JSON would trip
    the confirmed-image refusal without having changed anything.
    """
    return {
        str(code): [[[float(x), float(y)] for x, y in polygon] for polygon in polygons]
        for code, polygons in sorted((teeth or {}).items())
    }


def tooth_segmentation_state(patient, *, domain_field):
    """The latest polygons per file, their confirmation, and the revision a save must quote.

    Rebuilt from the canonical items rather than a payload -- see the module note. Only the
    latest revision is read: revisions are the audit trail and stay in the database, but
    they are not a concept the editor exposes, and falling back to an older one would
    resurrect polygons somebody deleted.

    Confirmation comes from the *target*, not the revision, because that is where a
    per-image claim lives (migration ``0003``) -- and it is reported for every target the
    set has, including images whose polygons were all deleted, so a confirmed-but-empty
    image still reads as confirmed instead of vanishing.

    ``updatedAt`` is the set's, not the image's. The new model has no per-image edit
    timestamp: items carry forward as fresh rows on every revision, so their age says when
    the last save happened, not when *that image* last changed. Reporting a set-level
    timestamp is the honest version of a number the legacy row happened to have per image.

    :returns: ``{"revision": int, "setId": int|None, "images": {file_id: teeth},
        "confirmations": {file_id: bool}, "updatedAt": datetime|None}``
    """
    from annotations.models import AnnotationSet, Geometry2DItem

    annotation_set = (
        AnnotationSet.objects.filter(**{domain_field: patient, "kind": SEGMENTATION_KIND})
        .order_by("id")
        .first()
    )
    empty = {
        "revision": 0,
        "setId": None,
        "images": {},
        "confirmations": {},
        "updatedAt": None,
    }
    if annotation_set is None:
        return empty

    confirmations = {
        target.source_resource.file_id: target.status == AnnotationStatus.CONFIRMED
        for target in annotation_set.targets.select_related("source_resource")
        if target.source_resource.file_id is not None
    }

    revision = annotation_set.revisions.order_by("-revision_number").first()
    if revision is None:
        return {
            **empty,
            "setId": annotation_set.id,
            "confirmations": confirmations,
            "updatedAt": annotation_set.updated_at,
        }

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

    stored = {file_id: teeth_from_items(rows) for file_id, rows in by_file.items()}
    return {
        "revision": revision.revision_number,
        "setId": annotation_set.id,
        "everAnnotated": annotation_set.ever_annotated,
        "images": _with_edited_descendants(patient, stored),
        "confirmations": confirmations,
        "updatedAt": annotation_set.updated_at,
    }


def _with_edited_descendants(patient, stored):
    """Add an entry for each photograph produced by *editing* one that has polygons.

    ``rgb_editor.js`` writes a new ``FileRegistry`` row when a photograph is cropped,
    mirrored or rotated, and records the operations in ``metadata['edit_meta']`` and the
    original's id in ``metadata['source_file_id']``. The polygons stay attached to the row
    they were drawn on, so without this the edited photograph reads back as unsegmented and
    the original reads back as segmented but is no longer the image anybody looks at.

    The legacy read did exactly this (``patient_intraoral_segmentation.py:437-450``), and
    the replay it used is now :mod:`annotations.adapters.image_edit_replay`. Keeping it on
    the *read* rather than rewriting the stored geometry is deliberate: a re-projection is
    derived, and the roadmap records seven production studies whose rotations were never
    applied and which a person still has to look at. Silently rewriting their polygons here
    would file a machine's guess as if somebody had approved it.

    A row that already has its own polygons is left alone -- somebody has drawn on the
    edited image, and their work is the answer.
    """
    if not stored:
        return stored

    descendants = {}
    for row in patient.files.filter(metadata__source_file_id__in=list(stored)).only(
        "id", "metadata"
    ):
        metadata = row.metadata if isinstance(row.metadata, dict) else {}
        source_id = metadata.get("source_file_id")
        if source_id in stored and row.id not in stored:
            descendants[row.id] = transform_teeth(
                stored[source_id], metadata.get("edit_meta")
            )

    # Empty results dropped rather than stored as `{}`: a crop can remove every tooth from
    # a photograph, and "this image has no polygons" is what no entry already means.
    return {**stored, **{key: value for key, value in descendants.items() if value}}
