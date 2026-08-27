"""Saving one viewer session's measurements.

The orchestration behind the volume grid's save. Everything it does is a call into
another service -- it exists so that "save the measurements on screen" is one
transaction with one name, rather than six calls a view is trusted to make in the
right order. A view that got the order wrong would still produce rows; they would
simply be rows with no target fingerprint, or a revision that never flipped
``ever_annotated``, and the failure would surface months later as a scan that turned
out to be replaceable.

Note what this is *not*: a ``POST /cornerstone/save``. The governing architectural rule
says the REST API is domain-oriented, and it is honoured here in substance and not just
in the URL. What arrives is translated into descriptors by
:mod:`annotations.adapters.cornerstone` before anything is written, every number is
recomputed from the geometry, and the viewer's own serialized state goes in as an
**editable, non-canonical** payload whose only job is to let the user resume editing.
If Cornerstone were replaced tomorrow, the canonical items would still be readable and
only the scratch payload would be dead.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from annotations.adapters import cornerstone as cs_adapter
from annotations.constants import (
    AnnotationOrigin,
    CoordinateSystem,
    PayloadFormat,
)
from annotations.constants import ResourceKind
from annotations.services.apply import apply_descriptors
from annotations.services.items import copy_items_to_revision
from annotations.services.resources import register_file, register_logical_volume
from annotations.services.sets import (
    add_payload,
    attach_target,
    get_or_create_set,
    record_revision,
)

#: How a group's resource is registered. A volume is "this voxel grid with this affine",
#: which is what a patient-space coordinate was measured against; a photograph is just
#: bytes. They get different identity namespaces on purpose, so the two can never
#: collide on one ``FileRegistry`` row.
_REGISTRARS = {
    ResourceKind.LOGICAL_VOLUME: register_logical_volume,
    ResourceKind.FILE: register_file,
}

#: The set kind measurements are filed under.
MEASUREMENTS_KIND = "measurements"

#: A save with more annotations than this is refused rather than written.
#:
#: Not a performance guard -- it is a corruption guard. Each annotation becomes two or
#: three rows in one transaction, and a client bug that resends its buffer in a loop
#: would otherwise turn a stuck save button into an unbounded write. A real session has
#: tens of measurements, not thousands.
MAX_ANNOTATIONS_PER_REVISION = 500


@transaction.atomic
def save_measurements(
    patient,
    *,
    file_obj,
    annotations,
    file_key=None,
    author=None,
    expected_revision=None,
    coordinate_system=CoordinateSystem.PATIENT_LPS_MM,
    volume_descriptor=None,
    annotation_method=None,
    note="",
):
    """Write one revision holding every measurement currently on screen.

    Replace-the-whole-set semantics, deliberately: a revision *is* the state of the
    work at a moment, and the viewer knows what is on screen far more reliably than it
    knows which individual annotation the user deleted. Diffing would mean trusting a
    client-side identity for each annotation -- which would have to be the
    ``annotationUID``, the one identifier the governing rule says is never persisted.

    :param patient: a domain ``Patient``.
    :param file_obj: the ``FileRegistry`` row the volume came from.
    :param annotations: the viewer's annotation list, as Cornerstone produced it.
    :param file_key: the bundle member, when the volume is one.
    :param expected_revision: the revision number the client loaded. A stale value
        raises :class:`~annotations.services.exceptions.AnnotationConflict`, which the
        view reports as 409 -- the optimistic-concurrency primitive is the unique
        constraint on ``(annotation_set, revision_number)``, not a ``SELECT MAX``.
    :param volume_descriptor: the grid facts (shape, spacing, affine) the coordinates
        were measured against, so ``annotations_crosscheck`` can later notice the
        volume being resampled underneath them.
    :returns: the new ``AnnotationRevision``.
    """
    return save_measurement_groups(
        patient,
        groups=[
            {
                "file_obj": file_obj,
                "file_key": file_key,
                "annotations": annotations,
                "descriptor": volume_descriptor or {},
                "resource_kind": ResourceKind.LOGICAL_VOLUME,
            }
        ],
        author=author,
        expected_revision=expected_revision,
        coordinate_system=coordinate_system,
        annotation_method=annotation_method,
        note=note,
    )


@transaction.atomic
def save_measurement_groups(
    patient,
    *,
    groups,
    primary_index=0,
    author=None,
    expected_revision=None,
    coordinate_system=CoordinateSystem.PATIENT_LPS_MM,
    annotation_method=None,
    note="",
    reclaim_primary=True,
    carry_forward=True,
    kind=MEASUREMENTS_KIND,
    label_schema=None,
    require_labels=False,
    translate=None,
    store_payload=True,
):
    """Write one revision holding the state of every resource the caller names.

    One group is one annotatable resource and the annotations drawn on it::

        {"file_obj": <FileRegistry>, "annotations": [...], "file_key": None,
         "descriptor": {...}, "resource_kind": ResourceKind.LOGICAL_VOLUME,
         "role": "", "order": 0}

    **Why this exists.** ``AnnotationSet`` is keyed ``(domain, patient, kind)`` and a
    revision replaces the whole set. That is right while one viewer holds the whole set,
    and wrong as soon as a patient owns more than one annotatable resource -- a photo
    stack of N images, or simply a CBCT *and* a teleradiography. The viewer saves what is
    on screen, and everything else would vanish from the new revision in a way
    indistinguishable from a deliberate deletion.

    So a save is: replace the groups named, and carry the rest forward
    (:func:`~annotations.services.items.copy_items_to_revision`). Both halves land on one
    revision inside one transaction, because a revision *is* the state of the work at a
    moment and a half-written one would be a state that never existed.

    ``reclaim_primary=False`` leaves the primary slot where it is. Without it a patient
    with both a volume and photographs would have the primary target ping-pong between
    modalities on every save, and the slot is meant to answer "what is this set mostly
    about", not "what was saved last".

    ``kind`` and ``require_labels`` are what let a second surface reuse all of this. Tooth
    segmentation is filed under its own kind -- a tooth polygon is not a measurement, and
    ``get_or_create_set``'s ``(domain, patient, kind)`` key therefore gives it its own set
    for free -- and it requires labels, because an FDI code is what decides a polygon's
    segment number on export. Measurements do not: a length is meaningful unlabelled.

    ``translate`` replaces the Cornerstone adapter for a caller whose input is not
    Cornerstone annotation state. It takes one group and returns its descriptors.

    :param primary_index: which group claims the primary slot, when one is claimed.
    :param carry_forward: set false only for a caller that genuinely owns the whole set.
    :param store_payload: false for a surface whose items are its only representation --
        a resumable scratch copy that nothing reads is a second thing to keep in step.
    :returns: the new ``AnnotationRevision``.
    """
    groups = list(groups or [])
    if not groups:
        raise ValidationError("a save must name at least one resource")
    if not 0 <= primary_index < len(groups):
        raise ValidationError("primary_index does not name one of the groups given")

    total = 0
    for index, group in enumerate(groups):
        annotations = group.get("annotations")
        if not isinstance(annotations, (list, tuple)):
            raise ValidationError(f"group {index}: annotations must be a list")
        if group.get("file_obj") is None:
            raise ValidationError(f"group {index}: no file to anchor the annotations to")
        total += len(annotations)
    # Counted across the whole save, not per group: the guard is against a client
    # resending its buffer, and a loop that does so would spread it over the groups.
    if total > MAX_ANNOTATIONS_PER_REVISION:
        raise ValidationError(
            f"{total} annotations in one save exceeds the "
            f"{MAX_ANNOTATIONS_PER_REVISION} limit; this is almost certainly a client "
            "resending its buffer rather than a real session"
        )

    # Translate every group first, write second. An annotation the adapter refuses -- an
    # unmapped tool, an incomplete handle set, a NaN coordinate -- must abort the whole
    # save before any row exists, or the user is left with a revision that silently holds
    # some of what was on screen. With several groups this matters more, not less: a
    # partial write would also look like a deletion on the groups that never got written.
    translated = [
        translate(group) if translate else _translate_cornerstone(group, coordinate_system)
        for group in groups
    ]

    annotation_set = get_or_create_set(
        patient,
        kind,
        annotation_method=annotation_method,
        label_schema=label_schema,
        created_by=author,
    )
    # Read before the new revision exists; afterwards "the latest" would be the empty
    # one being written.
    previous_revision = annotation_set.revisions.order_by("-revision_number").first()

    # `reclaim_primary=False` means "do not move a primary somebody else holds", not
    # "leave the set without one". A set with no primary target has no answer to "what is
    # this mostly about", which is the question the slot exists to hold -- so an unset
    # slot is still claimed, and only an already-set one is left alone. Without this
    # split, a photo save either steals the slot from a volume on every save or a
    # patient with only photographs never gets one at all.
    has_primary = annotation_set.targets.filter(primary_slot=1).exists()
    claim_primary = reclaim_primary or not has_primary

    targets = []
    for index, group in enumerate(groups):
        register = _REGISTRARS[group.get("resource_kind") or ResourceKind.LOGICAL_VOLUME]
        resource = register(
            group["file_obj"],
            file_key=group.get("file_key"),
            descriptor=group.get("descriptor") or {},
        )
        targets.append(
            attach_target(
                annotation_set,
                resource,
                role=group.get("role", ""),
                primary=claim_primary and index == primary_index,
                order=group.get("order", index),
            )
        )

    revision = record_revision(
        annotation_set,
        expected_revision=expected_revision,
        author=author,
        origin=AnnotationOrigin.MANUAL,
        note=note,
    )

    for target, descriptor_list in zip(targets, translated):
        apply_descriptors(revision, target, descriptor_list, require_labels=require_labels)

    if carry_forward:
        named = {target.pk for target in targets}
        untouched = [
            target for target in annotation_set.targets.all() if target.pk not in named
        ]
        copy_items_to_revision(previous_revision, revision, targets=untouched)

    # The resumable scratch copy. Never canonical, free to go stale, and stripped of
    # every runtime identifier on the way in.
    if store_payload:
        add_payload(
            revision,
            format=PayloadFormat.CORNERSTONE_STATE,
            data=_state_payload(groups, targets, previous_revision, carry_forward),
            canonical=False,
        )

    return revision


def _translate_cornerstone(group, coordinate_system):
    """The default translation: Cornerstone annotation state to descriptors."""
    descriptor_list = []
    for order, entry in enumerate(group["annotations"]):
        descriptor_list.extend(
            cs_adapter.descriptors_for_annotation(
                entry, coordinate_system=coordinate_system, order=order
            )
        )
    return descriptor_list


def _state_payload(groups, targets, previous_revision, carry_forward):
    """The ``cornerstone_state`` payload: per resource, plus the flat legacy key.

    ``annotations`` is emitted **only for a single-group save**, so the volume grid and
    ``measurements_state_api`` see byte-for-byte what they saw before. ``images`` is
    always emitted and is what a multi-resource viewer reads.

    Groups the save did not name have their payload entries copied forward verbatim, to
    match what :func:`~annotations.services.items.copy_items_to_revision` did to their
    canonical rows. The two must agree: a resume point that disagrees with the record is
    how a user comes back to a study and finds work missing that the database still has.
    """
    images = [
        {
            "fileId": group["file_obj"].pk,
            "fileKey": group.get("file_key") or None,
            "annotations": cs_adapter.strip_runtime_identifiers(list(group["annotations"])),
        }
        for group in groups
    ]

    if carry_forward and previous_revision is not None:
        named = {(entry["fileId"], entry["fileKey"]) for entry in images}
        payload = previous_revision.payloads.filter(
            format=PayloadFormat.CORNERSTONE_STATE
        ).first()
        for entry in (payload.data or {}).get("images", []) if payload else []:
            if not isinstance(entry, dict):
                continue
            if (entry.get("fileId"), entry.get("fileKey")) not in named:
                images.append(entry)

    state = {"images": images}
    if len(groups) == 1:
        state["annotations"] = images[0]["annotations"]
    return state
