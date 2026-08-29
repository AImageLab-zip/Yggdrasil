"""Registering the content annotations are anchored to.

The one rule: a set of bytes has exactly one :class:`SourceResource`. Two rows
for one volume would split its annotations between them, and nothing downstream
would notice -- each half would look complete. So registration is
get-or-create on the identity key, and the identity key comes from
``annotations.identity`` rather than from the caller.
"""

from django.db import transaction

from annotations import identity
from annotations.constants import ResourceKind
from annotations.models import SourceResource


def _update_if_changed(resource, **fields):
    """Refresh descriptive fields without touching identity.

    ``identity_key`` and ``kind`` are never updated here. Changing either would
    silently repoint every annotation hanging off the row, which is the one
    thing this module exists to prevent.
    """
    changed = [
        name for name, value in fields.items() if value and getattr(resource, name) != value
    ]
    if changed:
        for name in changed:
            setattr(resource, name, fields[name])
        resource.save(update_fields=[*changed, "updated_at"])
    return resource


@transaction.atomic
def register_file(file_obj, *, file_key=None, content_hash=None, descriptor=None):
    """Register a ``FileRegistry`` row, or one keyed member of its bundle."""
    key = identity.for_file(file_obj.pk, file_key)
    resource, _ = SourceResource.objects.get_or_create(
        identity_key=key,
        defaults={
            "kind": ResourceKind.FILE,
            "file": file_obj,
            "file_key": (file_key or "").strip(),
            "content_hash": content_hash or "",
            "descriptor": descriptor or {},
        },
    )
    return _update_if_changed(
        resource, content_hash=content_hash or "", descriptor=descriptor or {}
    )


@transaction.atomic
def register_logical_volume(file_obj, *, file_key=None, content_hash=None, descriptor=None):
    """Register the *volume* inside a file, which is not the same as the file.

    Separate from :func:`register_file` because the two carry different
    promises. A file resource says "these bytes"; a volume resource says "this
    voxel grid with this affine", which is what stored coordinates were measured
    against. A ``cbct_processed`` bundle holds a volume and a segmentation, and
    both are volumes in their own right.

    ``descriptor`` should carry the grid facts -- shape, spacing, the affine --
    so ``annotations_crosscheck`` can spot a volume that was resampled or
    re-oriented underneath its annotations without downloading it.
    """
    key = identity.for_logical_volume(file_obj.pk, file_key)
    resource, _ = SourceResource.objects.get_or_create(
        identity_key=key,
        defaults={
            "kind": ResourceKind.LOGICAL_VOLUME,
            "file": file_obj,
            "file_key": (file_key or "").strip(),
            "content_hash": content_hash or "",
            "descriptor": descriptor or {},
        },
    )
    return _update_if_changed(
        resource, content_hash=content_hash or "", descriptor=descriptor or {}
    )


@transaction.atomic
def register_dicom_series(series, *, descriptor=None):
    """Register a stored DICOM series as something annotations can anchor to.

    The DICOM counterpart of :func:`register_logical_volume`, and it exists for the
    same reason: a series *is* a voxel grid with an affine, and that grid is what
    stored coordinates were measured against.

    Two fields are populated here that no other resource kind carries.
    ``series_instance_uid`` is the durable name -- globally unique by construction, so
    unlike a ``FileRegistry`` id it survives the row being re-created.
    ``frame_of_reference_uid`` is the one that matters clinically: coordinates in
    ``patient_lps_mm`` are only comparable *within* one frame of reference, and without
    it on the record two series from one study look interchangeable when they are not.

    ``content_hash`` is the series' ``FileRegistry.file_hash`` -- a digest over the
    members' digests -- so ``annotations_crosscheck`` sees an instance being rewritten
    underneath its annotations, which the per-row lock cannot.
    """
    key = identity.for_dicom_series(series.series_instance_uid)
    defaults = {
        "kind": ResourceKind.DICOM_SERIES,
        # Deliberately set: the series is stored *as* a FileRegistry prefix row, so the
        # authorization funnel and the raw-data lock both reach it the ordinary way.
        "file": series.file,
        "series_instance_uid": series.series_instance_uid,
        "frame_of_reference_uid": series.frame_of_reference_uid,
        "content_hash": series.file.file_hash or "",
        "descriptor": descriptor or {},
    }
    resource, created = SourceResource.objects.get_or_create(
        identity_key=key, defaults=defaults
    )
    if not created:
        for field in ("series_instance_uid", "frame_of_reference_uid"):
            setattr(resource, field, defaults[field])
        resource.save(update_fields=["series_instance_uid", "frame_of_reference_uid"])
    return _update_if_changed(
        resource,
        content_hash=defaults["content_hash"],
        descriptor=descriptor or {},
    )


@transaction.atomic
def register_derived(
    producer, source_resource, *, discriminator=None, file_obj=None, descriptor=None
):
    """Register something computed from another resource.

    A panoramic strip is only interpretable together with the volume and arch it
    was baked from, so its identity embeds the source's. That means the same
    producer run against a different volume gets a different resource, which is
    what stops one patient's strip from being mistaken for another's.
    """
    key = identity.for_derived_resource(
        producer, source_resource.identity_key, discriminator
    )
    resource, _ = SourceResource.objects.get_or_create(
        identity_key=key,
        defaults={
            "kind": ResourceKind.DERIVED_RESOURCE,
            "file": file_obj,
            "descriptor": descriptor or {},
        },
    )
    return _update_if_changed(resource, descriptor=descriptor or {})


def fingerprint_targets(annotation_set):
    """``{identity_key: content_hash}`` for a set's targets, as they stand now.

    Stored on each revision. It does not prove an annotation is right, but a
    later mismatch proves nobody can claim it is: the bytes moved underneath it.
    Resources with no recorded hash are included with an empty string rather
    than omitted, so "we never knew" stays distinguishable from "it changed".
    """
    return {
        target.source_resource.identity_key: target.source_resource.content_hash
        for target in annotation_set.targets.select_related("source_resource")
    }
