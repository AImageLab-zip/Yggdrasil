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
from annotations.services.apply import apply_descriptors
from annotations.services.resources import register_logical_volume
from annotations.services.sets import (
    add_payload,
    attach_target,
    get_or_create_set,
    record_revision,
)

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
    if not isinstance(annotations, (list, tuple)):
        raise ValidationError("annotations must be a list")
    if len(annotations) > MAX_ANNOTATIONS_PER_REVISION:
        raise ValidationError(
            f"{len(annotations)} annotations in one save exceeds the "
            f"{MAX_ANNOTATIONS_PER_REVISION} limit; this is almost certainly a client "
            "resending its buffer rather than a real session"
        )

    # Translate first, write second. An annotation the adapter refuses -- an unmapped
    # tool, an incomplete handle set, a NaN coordinate -- must abort the whole save
    # before any row exists, or the user is left with a revision that silently holds
    # some of what was on screen.
    descriptor_list = []
    for order, entry in enumerate(annotations):
        descriptor_list.extend(
            cs_adapter.descriptors_for_annotation(
                entry, coordinate_system=coordinate_system, order=order
            )
        )

    annotation_set = get_or_create_set(
        patient,
        MEASUREMENTS_KIND,
        annotation_method=annotation_method,
        created_by=author,
    )

    # The *volume*, not the file: a measurement was taken against a voxel grid with an
    # affine, and a `cbct_processed` bundle holds several of those behind one file row.
    resource = register_logical_volume(
        file_obj, file_key=file_key, descriptor=volume_descriptor or {}
    )
    target = attach_target(annotation_set, resource, primary=True)

    revision = record_revision(
        annotation_set,
        expected_revision=expected_revision,
        author=author,
        origin=AnnotationOrigin.MANUAL,
        note=note,
    )

    # Labels are not required: a measurement is meaningful without one, unlike a tooth
    # polygon whose FDI code decides its export segment.
    apply_descriptors(revision, target, descriptor_list, require_labels=False)

    # The resumable scratch copy. Never canonical, free to go stale, and stripped of
    # every runtime identifier on the way in.
    add_payload(
        revision,
        format=PayloadFormat.CORNERSTONE_STATE,
        data=cs_adapter.cornerstone_state_payload(annotations),
        canonical=False,
    )

    return revision
