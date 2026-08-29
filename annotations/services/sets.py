"""Writing annotation work. The only layer permitted to save.

A view that imports a model and calls ``.save()`` is a review failure, and the
reason is here: every write has to allocate a revision number, refresh the
monotonic ``ever_annotated`` flag, fingerprint the targets and validate the
items, all in one transaction. Spread that across three views and one of them
will forget the flag, and the raw-data lock will let somebody replace a scan
that has landmarks on it.

The concurrency contract is worth reading before using :func:`record_revision`.
A caller that loaded revision 7 passes ``expected_revision=7``; the write
inserts revision 8, and if another writer got there first the unique constraint
refuses and the caller gets :class:`AnnotationConflict`. There is no
read-then-write window, because the check *is* the write -- no ``SELECT
MAX(...)`` to lose a race against. Passing ``expected_revision=None`` opts out
and appends after whatever is there; that is for migrations and imports, which
have no concurrent editor to lose to.
"""

from django.db import IntegrityError, transaction
from django.db.models import Max

from annotations.constants import (
    AnnotationOrigin,
    AnnotationStatus,
    PayloadFormat,
    ResourceKind,
)
from annotations.models import (
    AnnotationPayload,
    AnnotationRevision,
    AnnotationSet,
    AnnotationTarget,
)
from annotations.services.exceptions import AnnotationConflict, AnnotationNotAllowed
from annotations.services.resources import fingerprint_targets
from common.permissions import project_allows_annotation


def _method_slug(annotation_method):
    return getattr(annotation_method, "slug", None)


@transaction.atomic
def get_or_create_set(
    patient,
    kind,
    *,
    annotation_method=None,
    label_schema=None,
    created_by=None,
    check_project=True,
):
    """Find or start the set holding this kind of work for this patient.

    The project gate is checked here rather than left to each caller, because
    "every annotation write asks the project first" has already failed once by
    being a convention instead of a code path -- ``update_classification`` was
    the endpoint that never asked (F11). A service that cannot be reached
    without passing the gate cannot repeat that.

    ``check_project=False`` exists for the migration commands, which convert
    work that predates the project registry and must not silently drop rows
    belonging to a project that has since switched the method off.
    """
    slug = _method_slug(annotation_method)
    if check_project and slug and not project_allows_annotation(patient, slug):
        raise AnnotationNotAllowed(f"{slug} is disabled for this project")

    domain = patient._meta.app_label
    lookup = {"domain": domain, "kind": kind}
    lookup[{"maxillo": "patient", "brain": "brain_patient"}.get(domain, "laparoscopy_patient")] = patient

    annotation_set = AnnotationSet.objects.filter(**lookup).order_by("id").first()
    if annotation_set is not None:
        return annotation_set

    annotation_set = AnnotationSet(
        kind=kind,
        annotation_method=annotation_method,
        label_schema=label_schema,
        created_by=created_by,
    )
    annotation_set.set_patient(patient)
    annotation_set.save()
    return annotation_set


@transaction.atomic
def attach_target(annotation_set, source_resource, *, role="", primary=False, order=0):
    """Anchor a set to a resource.

    ``primary=True`` moves the primary slot rather than adding a second one:
    the unique constraint would refuse the insert otherwise, and refusing here
    would make "re-anchor this set" impossible without raw SQL.
    """
    if primary:
        annotation_set.targets.filter(primary_slot=1).update(primary_slot=None)

    target, created = AnnotationTarget.objects.get_or_create(
        annotation_set=annotation_set,
        source_resource=source_resource,
        role=role,
        defaults={"primary_slot": 1 if primary else None, "order": order},
    )
    if not created and primary and target.primary_slot != 1:
        target.primary_slot = 1
        target.save(update_fields=["primary_slot"])
    return target


@transaction.atomic
def set_target_status(target, status):
    """Move one target's lifecycle status, or clear it with ``None``.

    Separate from :func:`attach_target` on purpose. Anchoring a set to a resource and
    claiming the work on it is reviewed are different statements, and a combined
    signature would let a save that never mentioned confirmation clear it as a side
    effect of re-anchoring -- which is how a confirmed photograph silently becomes
    editable again.
    """
    if status is not None and status not in AnnotationStatus.ALL:
        raise ValueError(f"unknown annotation status {status!r}")
    if target.status != status:
        target.status = status
        target.save(update_fields=["status"])
    return target


def current_revision_number(annotation_set):
    """The highest revision number on this set, or 0 when it has none.

    Read-only and safe to show a client, but never use it to *compute* the next
    number for a write -- pass what the client loaded to
    :func:`record_revision` instead, or the race the unique constraint exists to
    catch reopens in application code.
    """
    return (
        annotation_set.revisions.aggregate(highest=Max("revision_number"))["highest"] or 0
    )


@transaction.atomic
def record_revision(
    annotation_set,
    *,
    expected_revision=None,
    author=None,
    origin=AnnotationOrigin.MANUAL,
    note="",
    status=None,
):
    """Append one revision, refusing to overwrite somebody else's work.

    Returns the new :class:`AnnotationRevision`. Items and payloads are written
    against it by the caller, inside this same transaction, via
    :mod:`annotations.services.items` and :func:`add_payload`.

    Raises :class:`AnnotationConflict` when ``expected_revision`` is stale.
    """
    if expected_revision is None:
        next_number = current_revision_number(annotation_set) + 1
    else:
        next_number = int(expected_revision) + 1

    try:
        # A savepoint, so a conflict does not poison the caller's transaction:
        # a failed INSERT leaves the outer atomic block unusable otherwise, and
        # the caller needs to be able to report the 409 and roll back cleanly.
        with transaction.atomic():
            revision = AnnotationRevision.objects.create(
                annotation_set=annotation_set,
                revision_number=next_number,
                origin=origin,
                author=author,
                note=note,
                source_fingerprint=fingerprint_targets(annotation_set),
            )
    except IntegrityError as exc:
        raise AnnotationConflict(
            f"revision {next_number} already exists; reload and reapply"
        ) from exc

    updates = ["updated_at"]
    # Monotonic, per decision #18: once human work exists the patient's raw
    # inputs are frozen for good, and deleting the work does not thaw them. The
    # flag is only ever turned on, so a later prediction or an empty revision
    # cannot quietly unfreeze a case.
    if origin in AnnotationOrigin.HUMAN and not annotation_set.ever_annotated:
        annotation_set.ever_annotated = True
        updates.append("ever_annotated")
        _seal_dicom_sources(annotation_set)
    if status is not None and status != annotation_set.status:
        annotation_set.status = status
        updates.append("status")
    annotation_set.save(update_fields=updates)

    return revision


def _seal_dicom_sources(annotation_set):
    """Freeze any DICOM series this set is anchored to, the first time a human writes.

    ``ever_annotated`` freezes the patient's raw *rows*, which is the whole lock for a
    NIfTI scan: one row, one file. A DICOM series is one row holding several hundred
    objects, and rewriting instance 137 in place would re-base every coordinate drawn
    on the volume without touching the row at all. ``sealed_at`` is what closes that,
    and this is the moment it has to close -- the same moment, and for the same reason,
    that the raw data itself stops being replaceable.

    Machine output does not seal, because it does not set ``ever_annotated``: a
    prediction over a series must not stop a correction being ingested later.
    """
    from common.dicom.models import DicomSeries

    series_uids = list(
        annotation_set.targets.filter(
            source_resource__kind=ResourceKind.DICOM_SERIES
        ).values_list("source_resource__series_instance_uid", flat=True)
    )
    if not series_uids:
        return
    for series in DicomSeries.objects.filter(
        series_instance_uid__in=series_uids, sealed_at__isnull=True
    ):
        series.seal()


@transaction.atomic
def add_payload(
    revision, *, format, data=None, file_obj=None, variant="", canonical=False, content_hash="", byte_size=None
):
    """Attach one encoding of a revision's content.

    Exactly one of ``data`` and ``file_obj``: the database refuses a payload
    that holds neither (a row claiming content it does not have) or both
    (ambiguous about which copy is real). Dense voxels are always ``file_obj``.

    ``canonical=True`` on a second payload is refused by the unique slot rather
    than silently demoting the first, because "which one is the truth" is not a
    question a write should answer by guessing.
    """
    if format not in PayloadFormat.ALL:
        raise ValueError(f"unknown payload format {format!r}")
    # Never canonical, by design: a viewer's serialized state is an editable
    # scratch copy that is allowed to drift from the items.
    if canonical and format == PayloadFormat.CORNERSTONE_STATE:
        raise ValueError("viewer state is never the canonical representation")

    return AnnotationPayload.objects.create(
        revision=revision,
        format=format,
        variant=variant,
        canonical_slot=1 if canonical else None,
        data=data,
        file=file_obj,
        content_hash=content_hash,
        byte_size=byte_size,
    )


@transaction.atomic
def confirm(annotation_set):
    """Mark a set reviewed. Does not freeze it; ``ever_annotated`` already did."""
    if annotation_set.status != AnnotationStatus.CONFIRMED:
        annotation_set.status = AnnotationStatus.CONFIRMED
        annotation_set.save(update_fields=["status", "updated_at"])
    return annotation_set
