"""Saving and reading the panoramic arch, through the machinery measurements use.

Thin on purpose, and for the same reasons :mod:`annotations.services.segmentation` and
:mod:`annotations.services.ios_landmarks` are. Everything structural -- one revision per
save, the optimistic-concurrency check, target fingerprinting, the monotonic lock -- is
:func:`annotations.services.viewer.save_measurement_groups`. This module is what tells it
that the item is an arch rather than a measurement.

Five differences from a measurement save, each a real one:

**Its own set kind.** ``panoramic_arch`` was already an ``AnnotationSet.kind`` and already
what ``annotations_convert_legacy`` writes, so reusing it is what lets
``annotations_crosscheck`` find both representations of one study.

**No annotation method, deliberately.** Migration ``0001`` records it: the registry does
not gate this kind, because the arch's editability is governed by the annotation lock
instead. A reader who has annotated a case may no longer redraw its arch; a project that
has never heard of "panoramic" must still get one.

**Two targets, one of which carries no items.** The arch is drawn on the CBCT, but it is
*fitted* against the paired segmentation -- the mandible mask is what the polynomial goes
through. Both are attached, so ``AnnotationRevision.source_fingerprint`` covers both and a
re-run segmentation is visible as a changed source rather than silently leaving an arch
that was fitted to bytes nobody has any more.

**No scratch payload.** The control points *are* the arch. A second copy allowed to go
stale would only ever disagree with them.

**The baked strips ride along as payloads.** ``png_render`` exists for exactly this
(``AnnotationPayload.variant`` names which of the two), and it is what connects the PNGs
``common/export_catalog.py`` ships to the geometry they were produced from. Neither is
canonical: they are derived artifacts, regenerable from the arch, and saying otherwise
would make an image the truth about a curve.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from annotations import identity
from annotations.adapters.panoramic import (
    PANORAMIC_KIND,
    arch_from_items,
    panoramic_arch,
)
from annotations.constants import (
    AnnotationOrigin,
    CoordinateSystem,
    PayloadFormat,
    ResourceKind,
)
from annotations.services.sets import add_payload
from annotations.services.viewer import save_measurement_groups

#: The role the CBCT is anchored under.
#:
#: Byte-identical to what ``annotations_convert_legacy`` passes, and that is the whole
#: point. ``attach_target`` keys on ``(annotation_set, source_resource, role)``, so a live
#: save under a different role would create a *second* target for the same volume, and a
#: converted study edited once would read back with two arches. Phase 5 shipped a latent
#: defect of exactly that shape.
VOLUME_ROLE = "volume"

#: The role the paired segmentation is anchored under. It holds no items -- see the
#: module note.
SEGMENTATION_ROLE = "segmentation"

#: The two strips a save bakes, in the order the editor reports them.
STRIP_VARIANTS = ("mip", "raysum")


def arch_origin(geometry_source):
    """Who produced this arch, in the terms the lock understands.

    The single most consequential line in this module. ``panoramic_warmup`` drives every
    patient in a folder through an unattended save, and every one of those arches is
    ``auto``. Recorded as human work, one warm-up run would freeze the raw data of every
    case it touched -- irreversibly, because decision #18 made the lock monotonic.

    The converter makes the same distinction from the same field, which is why the two
    agree on a study that was converted and then edited.
    """
    return AnnotationOrigin.PREDICTION if geometry_source == "auto" else AnnotationOrigin.MANUAL


def expected_fingerprint(*, volume_file, volume_file_key, volume_hash,
                         segmentation_file=None, segmentation_file_key=None,
                         segmentation_hash=None):
    """``{identity_key: content_hash}`` for the source a save is about to claim.

    Pure -- it builds identity keys rather than reading resources -- so the state endpoint
    can ask "is the stored arch still about the CBCT we are serving?" without writing
    anything. Compared against ``AnnotationRevision.source_fingerprint``, which
    :func:`annotations.services.sets.record_revision` stamps on every revision.

    This replaces the seven-field comparison the legacy row carried. The processing job id
    is deliberately *not* part of it: what an arch was measured against is the bytes, and
    a re-run job that produced identical bytes has not invalidated anything.
    """
    fingerprint = {
        identity.for_logical_volume(volume_file.pk, volume_file_key): volume_hash or "",
    }
    if segmentation_file is not None:
        fingerprint[
            identity.for_logical_volume(segmentation_file.pk, segmentation_file_key)
        ] = segmentation_hash or ""
    return fingerprint


def arch_describes_source(state, *, volume_file, volume_file_key, volume_hash,
                          segmentation_file=None, segmentation_file_key=None,
                          segmentation_hash=None):
    """Whether the arch in *state* was drawn on the source described.

    Two questions, and both are needed.

    **Is it the same volume?** Answered from the arch item's own target, not from the
    set's. Targets accumulate -- replacing a CBCT leaves the old one attached as the
    audit trail of what earlier revisions were drawn on -- so "the set knows this volume"
    is a much weaker claim than "this arch is on it".

    **Are the bytes still the ones it was drawn against?** Answered from the revision's
    ``source_fingerprint``, which is a *superset*: it covers every target the set had when
    the revision was stamped. So each expected entry must be present with the hash it is
    expected to have, rather than the two maps being equal.

    Known residual: a study whose *segmentation* is replaced and then reverted reports a
    match, because the old segmentation's entry is still in the fingerprint. The volume
    half has no such hole, and the arch would be one fitted to the same mask either way.
    """
    if not state.get("arch"):
        return False
    expected = expected_fingerprint(
        volume_file=volume_file,
        volume_file_key=volume_file_key,
        volume_hash=volume_hash,
        segmentation_file=segmentation_file,
        segmentation_file_key=segmentation_file_key,
        segmentation_hash=segmentation_hash,
    )
    volume_key = identity.for_logical_volume(volume_file.pk, volume_file_key)
    if state.get("volumeIdentity") != volume_key:
        return False
    stored = state.get("fingerprint") or {}
    return all(stored.get(key) == value for key, value in expected.items())


@transaction.atomic
def save_panoramic_arch(
    patient,
    *,
    volume_file,
    spline,
    axial_slice,
    volume_shape,
    geometry_source,
    default_mode,
    algorithm_version,
    volume_file_key=None,
    volume_hash=None,
    segmentation_file=None,
    segmentation_file_key=None,
    segmentation_hash=None,
    strips=(),
    author=None,
    expected_revision=None,
    note="",
):
    """Write one revision holding the arch and the strips baked from it.

    :param spline: the control points, ``[[x, y], ...]`` in the axial slice's pixels.
    :param strips: ``[{"variant": "mip"|"raysum", "file_obj": <FileRegistry>,
        "content_hash": str, "byte_size": int}]``. Attached as ``png_render`` payloads.
    :param expected_revision: the revision number the client loaded. Stale raises
        :class:`~annotations.services.exceptions.AnnotationConflict` -> 409.
    :returns: the new ``AnnotationRevision``.
    """
    descriptor = {"volume_shape": list(volume_shape or [])}
    groups = [
        {
            "file_obj": volume_file,
            "file_key": volume_file_key,
            # Carried so the shared writer can count and validate uniformly; `translate`
            # below is what actually produces the item.
            "annotations": [],
            "descriptor": descriptor,
            "resource_kind": ResourceKind.LOGICAL_VOLUME,
            "role": VOLUME_ROLE,
            "order": 0,
        }
    ]
    if segmentation_file is not None:
        groups.append(
            {
                "file_obj": segmentation_file,
                "file_key": segmentation_file_key,
                "annotations": [],
                "descriptor": descriptor,
                "resource_kind": ResourceKind.LOGICAL_VOLUME,
                "role": SEGMENTATION_ROLE,
                "order": 1,
            }
        )

    def translate(group):
        if group.get("role") != VOLUME_ROLE:
            return []
        return panoramic_arch(
            spline,
            axial_slice=axial_slice,
            volume_shape=volume_shape,
            geometry_source=geometry_source,
            default_mode=default_mode,
            algorithm_version=algorithm_version,
        )

    # **Before** the write, not after. `record_revision` stamps `source_fingerprint`
    # from the resources' recorded hashes, and `save_measurement_groups` registers them
    # with the descriptor only -- so a hash applied afterwards would land one revision too
    # late, and the revision that first saw new bytes would claim the old ones.
    _record_source_hashes(
        volume_file, volume_file_key, volume_hash,
        segmentation_file, segmentation_file_key, segmentation_hash,
    )

    revision = save_measurement_groups(
        patient,
        groups=groups,
        author=author,
        expected_revision=expected_revision,
        coordinate_system=CoordinateSystem.SLICE_PIXEL,
        # No method: the registry does not gate this kind. See the module note.
        annotation_method=None,
        note=note,
        origin=arch_origin(geometry_source),
        kind=PANORAMIC_KIND,
        translate=translate,
        store_payload=False,
        # **Not carried forward.** A patient has exactly one arch and every save names
        # it, so this caller genuinely owns the whole set -- which is the condition the
        # shared writer documents for switching it off. Leaving it on would be a defect
        # with a long fuse: replacing the CBCT anchors the new arch to a *new* volume
        # resource, leaving the old target untouched, and the carry-forward would copy
        # its arch onto the same revision. `arch_from_items` reads the first polyline it
        # finds, so a study whose volume was replaced would come back holding an arch
        # drawn on bytes that no longer exist, with nothing about it looking wrong.
        carry_forward=False,
        # The volume claims the slot on the first save and keeps it. `reclaim_primary`
        # does not mean "no primary" -- the shared writer still claims an unset slot.
        reclaim_primary=False,
        primary_index=0,
    )

    for strip in strips:
        variant = strip.get("variant")
        if variant not in STRIP_VARIANTS:
            raise ValidationError(f"unknown panoramic strip variant {variant!r}")
        add_payload(
            revision,
            format=PayloadFormat.PNG_RENDER,
            file_obj=strip["file_obj"],
            variant=variant,
            # Neither strip is canonical: they are derived from the arch and regenerable
            # from it, and an image is not the truth about a curve.
            canonical=False,
            content_hash=strip.get("content_hash") or "",
            byte_size=strip.get("byte_size"),
        )
    return revision


def _record_source_hashes(volume_file, volume_file_key, volume_hash,
                          segmentation_file, segmentation_file_key, segmentation_hash):
    """Refresh each source resource's recorded content hash.

    The hash is descriptive, not part of the identity key (see
    ``annotations.services.resources``), so ``register_logical_volume`` inside the shared
    writer -- which is passed a descriptor and nothing else -- does not carry it. Doing it
    here, first, is what makes the revision's ``source_fingerprint`` describe the bytes
    this save actually read. ``_update_if_changed`` skips empty values, so the writer's
    later registration of the same identity key cannot blank what this set.
    """
    from annotations.services.resources import register_logical_volume

    register_logical_volume(
        volume_file, file_key=volume_file_key, content_hash=volume_hash or ""
    )
    if segmentation_file is not None:
        register_logical_volume(
            segmentation_file,
            file_key=segmentation_file_key,
            content_hash=segmentation_hash or "",
        )


def panoramic_arch_state(patient):
    """The stored arch, its strips, and the revision a save must quote.

    Rebuilt from the canonical items rather than a payload -- see the module note. Only
    the latest revision is read: revisions are the audit trail and stay in the database,
    but falling back to an older one would resurrect an arch somebody replaced.

    :returns: ``{"revision", "setId", "arch", "volumeIdentity", "strips", "fingerprint",
        "everAnnotated", "updatedAt"}``. ``arch`` is ``None`` for a patient who has never
        had one; ``strips`` maps ``"mip"``/``"raysum"`` to a ``FileRegistry`` row;
        ``volumeIdentity`` is the identity key of the resource the arch is drawn on.
    """
    from annotations.models import AnnotationSet, Geometry2DItem
    from common.domains import fk_fields_for

    patient_fk, _ = fk_fields_for(patient._meta.app_label)
    annotation_set = (
        AnnotationSet.objects.filter(**{patient_fk: patient, "kind": PANORAMIC_KIND})
        .order_by("id")
        .first()
    )
    empty = {
        "revision": 0,
        "setId": None,
        "arch": None,
        "volumeIdentity": None,
        "strips": {},
        "fingerprint": {},
        "everAnnotated": False,
        "updatedAt": None,
    }
    if annotation_set is None:
        return empty

    revision = annotation_set.revisions.order_by("-revision_number").first()
    if revision is None:
        # An empty set is what a crashed run leaves behind; it is not an arch.
        return {**empty, "setId": annotation_set.id, "updatedAt": annotation_set.updated_at}

    items = list(
        Geometry2DItem.objects.filter(revision=revision)
        .select_related("selector", "target__source_resource")
        .order_by("order", "id")
    )
    anchor = items[0].target if items else None
    strips = {
        payload.variant: payload.file
        for payload in revision.payloads.filter(
            format=PayloadFormat.PNG_RENDER
        ).select_related("file")
        if payload.file is not None
    }
    return {
        "revision": revision.revision_number,
        "setId": annotation_set.id,
        "arch": arch_from_items(items),
        "volumeIdentity": anchor.source_resource.identity_key if anchor else None,
        "strips": strips,
        "fingerprint": revision.source_fingerprint or {},
        "everAnnotated": annotation_set.ever_annotated,
        "updatedAt": annotation_set.updated_at,
    }
