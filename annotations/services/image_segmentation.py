"""Saving and reading dense 2D labelmaps: stills, and the frames of a video.

One ``AnnotationSet`` of kind ``image_segmentation`` per patient. Its **truth is pixels**
(decision: the labelmap is canonical; there are no stored strokes or polygons), kept as

- one ``npz_mask`` payload **per annotated frame**, non-canonical, whose ``variant`` is
  ``f<fileId>t<timeMs>`` and whose bytes are an ``.npz`` in object storage (one binary
  plane per label, so labels may overlap); and
- one canonical inline ``yggdrasil_json`` **manifest** per revision listing every frame
  that exists and a per-label ``{area, bbox}`` summary. The manifest is the index: "which
  frames have label X" is answered from it without opening a file.

A save names only the frames that were edited. Every other frame is **carried forward** by
a new payload row pointing at the *same* ``FileRegistry`` file -- a revision is a snapshot,
but it never copies bytes. A frame sent with no pixels set is a deletion: it is neither
written nor carried.

Frames are keyed by **milliseconds**, not frame index, so re-encoding a video does not
move a mask; the source file and its pixel grid are recorded with it, and a reader that
holds a different grid must refuse rather than resample.

The vocabulary is a per-project ``LabelSchema`` (``image-segmentation-project-<pk>``).
``LabelDefinition.value`` never changes once assigned and ``code`` (``l<value>``) is what
planes are keyed by, so a rename or a recolour cannot move a mask, and a label is *retired*
(``is_active=False``), never deleted, because deleting one would orphan its pixels.

Object storage is written **before** the database transaction and cleaned up if it fails;
an object that already exists (the same bytes saved again) is neither re-uploaded nor, on
failure, deleted, because an earlier revision may reference it.
"""

import copy
import hashlib
import io
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from annotations.adapters import image_segmentation as codec
from annotations.constants import AnnotationOrigin, CoordinateSystem, PayloadFormat
from annotations.models import AnnotationSet, LabelDefinition, LabelSchema
from annotations.services.exceptions import AnnotationConflict
from annotations.services.resources import register_file
from annotations.services.sets import (
    add_payload,
    attach_target,
    current_revision_number,
    get_or_create_set,
    record_revision,
)
from annotations.validators.image_segmentation import (
    MAX_LABELS_PER_FRAME,
    validate_color,
    validate_frame_batch,
    validate_time_ms,
)
from common.domains import fk_fields_for
from common.models import AnnotationMethod, FileRegistry

logger = logging.getLogger(__name__)

KIND = "image_segmentation"
#: The ``AnnotationMethod`` slug that gates this work for a project.
METHOD_SLUG = "image_segmentation"
MASK_FILE_TYPE = "annotation_mask"
MANIFEST_SCHEMA = 1

#: Colours handed to new labels in turn, ordered for contrast between neighbours.
PALETTE = (
    "#3498db", "#e74c3c", "#2ecc71", "#f39c12", "#9b59b6",
    "#1abc9c", "#e67e22", "#34495e", "#d81b60", "#7f8c8d",
)

NAME_MAX = 120
#: The default vocabulary: the project's segmentation labels.
VOCABULARY = "image-segmentation"


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


def project_label_schema(project, vocabulary=VOCABULARY):
    """The project's label schema, created on first use.

    Created on demand rather than seeded because the vocabulary is user-defined per
    project; unlike FDI there is nothing to review in a migration. ``vocabulary`` names
    which of the project's vocabularies this is, so another kind (the video quadrants)
    can keep its own labels without a second copy of the CRUD below.
    """
    schema, _created = LabelSchema.objects.get_or_create(
        slug=f"{vocabulary}-project-{project.pk}",
        version=1,
        defaults={
            "name": f"{vocabulary.replace('-', ' ').capitalize()} ({project.name})",
            "domain": "",
            "description": f"Per-project vocabulary: {vocabulary}.",
        },
    )
    return schema


def label_dict(definition):
    return {
        "code": definition.code,
        "value": definition.value,
        "name": definition.display_name,
        "color": definition.color,
        "order": definition.order,
        "active": definition.is_active,
    }


def list_labels(project, vocabulary=VOCABULARY):
    """Every label of the project, retired ones included and flagged."""
    if project is None:
        return []
    schema = project_label_schema(project, vocabulary)
    return [
        label_dict(definition)
        for definition in schema.definitions.order_by("order", "value")
    ]


def _clean_name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("a label needs a name")
    name = name.strip()
    if len(name) > NAME_MAX:
        raise ValidationError(f"a label name is at most {NAME_MAX} characters")
    return name


def _name_in_use(schema, name, *, exclude_pk=None):
    taken = schema.definitions.filter(is_active=True, display_name__iexact=name)
    if exclude_pk is not None:
        taken = taken.exclude(pk=exclude_pk)
    return taken.exists()


@transaction.atomic
def create_label(project, *, name, color="", vocabulary=VOCABULARY):
    """Add a label. Its integer ``value`` is never reused, even after retirement."""
    schema = LabelSchema.objects.select_for_update().get(
        pk=project_label_schema(project, vocabulary).pk
    )
    name = _clean_name(name)
    if _name_in_use(schema, name):
        raise ValidationError(f"there is already a label called {name!r}")
    if schema.definitions.count() >= MAX_LABELS_PER_FRAME:
        raise ValidationError(f"a project may define at most {MAX_LABELS_PER_FRAME} labels")
    value = (schema.definitions.aggregate(top=Max("value"))["top"] or 0) + 1
    definition = LabelDefinition.objects.create(
        schema=schema,
        value=value,
        code=f"l{value}",
        display_name=name,
        color=validate_color(color) or PALETTE[(value - 1) % len(PALETTE)],
        order=value,
    )
    return label_dict(definition)


@transaction.atomic
def update_label(project, code, *, name=None, color=None, active=None, vocabulary=VOCABULARY):
    """Rename, recolour, retire or restore a label. Never changes ``value`` or ``code``."""
    schema = LabelSchema.objects.select_for_update().get(
        pk=project_label_schema(project, vocabulary).pk
    )
    definition = schema.definitions.filter(code=code).first()
    if definition is None:
        raise LookupError(code)
    if name is not None:
        definition.display_name = _clean_name(name)
    if color is not None:
        definition.color = validate_color(color) or definition.color
    if active is not None:
        definition.is_active = bool(active)
    if definition.is_active and _name_in_use(
        schema, definition.display_name, exclude_pk=definition.pk
    ):
        raise ValidationError(
            f"there is already a label called {definition.display_name!r}"
        )
    definition.save()
    return label_dict(definition)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _method():
    """The registry row, or ``None`` if none is seeded (which leaves the work ungated)."""
    return AnnotationMethod.objects.filter(slug=METHOD_SLUG).first()


def _find_set(patient):
    domain = patient._meta.app_label
    patient_fk = fk_fields_for(domain)[0]
    return (
        AnnotationSet.objects.filter(domain=domain, kind=KIND, **{patient_fk: patient})
        .order_by("id")
        .first()
    )


def _latest_revision(annotation_set):
    if annotation_set is None:
        return None
    return annotation_set.revisions.order_by("-revision_number").first()


def _empty_manifest():
    return {"schema": MANIFEST_SCHEMA, "sources": {}}


def _manifest_of(revision):
    if revision is None:
        return _empty_manifest()
    payload = revision.payloads.filter(
        format=PayloadFormat.YGGDRASIL_JSON, canonical_slot=1
    ).first()
    return copy.deepcopy(payload.data) if payload and payload.data else _empty_manifest()


def image_segmentation_state(patient):
    """The vocabulary, the revision a save must quote, and which frames hold masks."""
    annotation_set = _find_set(patient)
    revision = _latest_revision(annotation_set)
    manifest = _manifest_of(revision)
    return {
        "revision": revision.revision_number if revision else 0,
        "setId": annotation_set.pk if annotation_set else None,
        "labels": list_labels(getattr(patient, "project", None)),
        "sources": manifest["sources"],
        "updatedAt": revision.created_at if revision else None,
    }


def read_frame(patient, file_id, time_ms):
    """``{"width", "height", "planes": {code: plane}}`` for one frame, or ``None``.

    Verifies the stored bytes against the hash recorded when they were written: a mask
    that quietly changed underneath its revision is corruption, and showing it as the
    annotator's work would be worse than failing.
    """
    from common.object_storage import get_object_storage

    revision = _latest_revision(_find_set(patient))
    if revision is None:
        return None
    payload = (
        revision.payloads.filter(
            format=PayloadFormat.NPZ_MASK, variant=codec.frame_variant(file_id, time_ms)
        )
        .select_related("file")
        .first()
    )
    if payload is None or payload.file is None:
        return None
    body, _info = get_object_storage().get(payload.file.file_path)
    try:
        content = body.read()
    finally:
        body.close()
    if payload.content_hash and hashlib.sha256(content).hexdigest() != payload.content_hash:
        raise RuntimeError(
            f"stored mask {payload.file.file_path} does not match its recorded hash"
        )
    width, height, planes = codec.npz_to_planes(content)
    return {"width": width, "height": height, "planes": planes}


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def _decode_frames(frames, known_codes):
    decoded = []
    for index, frame in enumerate(frames):
        planes = {}
        for code, encoded in frame["planes"].items():
            if code not in known_codes:
                raise ValidationError(f"frames[{index}]: unknown label {code!r}")
            plane = codec.decode_plane(encoded, frame["width"], frame["height"])
            if plane.any():
                planes[code] = plane
        decoded.append({**frame, "planes": planes})
    return decoded


def _store_frames(patient, source_file, decoded, uploaded):
    """Build each frame's archive and put new bytes in storage. Appends to ``uploaded``.

    Returns the prepared frames. A frame with no pixels carries no archive: it is a
    deletion.
    """
    from common.object_storage import get_object_storage

    storage = get_object_storage()
    prepared = []
    for frame in decoded:
        variant = codec.frame_variant(source_file.pk, frame["time_ms"])
        if not frame["planes"]:
            prepared.append({**frame, "variant": variant, "key": None})
            continue
        content = codec.planes_to_npz(frame["width"], frame["height"], frame["planes"])
        digest = hashlib.sha256(content).hexdigest()
        key = (
            f"annotations/image_segmentation/patient_{patient.patient_id}/"
            f"{variant}_{digest[:12]}.npz"
        )
        if not FileRegistry.objects.filter(file_path=key).exists():
            storage.upload_fileobj(
                io.BytesIO(content), key=key, content_type="application/octet-stream"
            )
            uploaded.append(key)
        prepared.append(
            {
                **frame,
                "variant": variant,
                "key": key,
                "digest": digest,
                "size": len(content),
                "labels": {
                    code: codec.plane_stats(plane) for code, plane in frame["planes"].items()
                },
            }
        )
    return prepared


def _discard(uploaded):
    from common.object_storage import get_object_storage

    storage = get_object_storage()
    for key in uploaded:
        try:
            storage.delete(key)
        except Exception:  # best effort: an orphan object is a storage cost, not an error
            logger.warning("could not remove orphaned mask object %s", key, exc_info=True)


@transaction.atomic
def _write_revision(
    patient, source_file, prepared, schema, *, author, expected_revision, coordinate_system, fps
):
    domain = patient._meta.app_label
    patient_fk = fk_fields_for(domain)[0]

    annotation_set = get_or_create_set(
        patient,
        KIND,
        annotation_method=_method(),
        label_schema=schema,
        created_by=author,
    )
    attach_target(
        annotation_set,
        register_file(source_file, content_hash=source_file.file_hash or ""),
        role="image",
    )
    previous = _latest_revision(annotation_set)
    manifest = _manifest_of(previous)

    revision = record_revision(
        annotation_set,
        expected_revision=expected_revision,
        author=author,
        origin=AnnotationOrigin.MANUAL,
    )

    # Carry forward: new payload rows over the *same* files, for every frame this save
    # did not name. No bytes are copied.
    named = {frame["variant"] for frame in prepared}
    if previous is not None:
        for old in previous.payloads.filter(format=PayloadFormat.NPZ_MASK).select_related("file"):
            if old.variant not in named:
                add_payload(
                    revision,
                    format=PayloadFormat.NPZ_MASK,
                    file_obj=old.file,
                    variant=old.variant,
                    content_hash=old.content_hash,
                    byte_size=old.byte_size,
                )

    source = manifest["sources"].setdefault(
        str(source_file.pk), {"coordinateSystem": coordinate_system, "frames": {}}
    )
    source["coordinateSystem"] = coordinate_system
    if fps:
        source["fps"] = fps
    for frame in prepared:
        key = str(frame["time_ms"])
        if frame["key"] is None:
            source["frames"].pop(key, None)
            continue
        file_row, _created = FileRegistry.objects.get_or_create(
            file_path=frame["key"],
            defaults={
                "domain": domain,
                "file_type": MASK_FILE_TYPE,
                "file_size": frame["size"],
                "file_hash": frame["digest"],
                "metadata": {"kind": KIND, "variant": frame["variant"]},
                patient_fk: patient,
            },
        )
        add_payload(
            revision,
            format=PayloadFormat.NPZ_MASK,
            file_obj=file_row,
            variant=frame["variant"],
            content_hash=frame["digest"],
            byte_size=frame["size"],
        )
        source["frames"][key] = {
            "variant": frame["variant"],
            "width": frame["width"],
            "height": frame["height"],
            "labels": frame["labels"],
        }
    manifest["sources"] = {
        file_id: entry for file_id, entry in manifest["sources"].items() if entry["frames"]
    }
    add_payload(
        revision, format=PayloadFormat.YGGDRASIL_JSON, data=manifest, canonical=True
    )
    return revision, manifest


def save_image_segmentation(
    patient,
    *,
    source_file,
    frames,
    author,
    expected_revision=None,
    coordinate_system=CoordinateSystem.VIDEO_PIXEL,
    fps=None,
):
    """Write the edited frames as a new revision. Returns ``(revision, manifest)``.

    ``frames`` is a list of ``{"time_ms", "width", "height", "planes"}`` where ``planes``
    maps a label code to its wire-encoded plane. Raises ``ValidationError`` (400, nothing
    written), ``AnnotationConflict`` (409) or ``AnnotationNotAllowed`` (403).
    """
    validate_frame_batch(frames)
    for index, frame in enumerate(frames):
        validate_time_ms(frame["time_ms"], where=f"frames[{index}].timeMs")
    if coordinate_system not in (CoordinateSystem.VIDEO_PIXEL, CoordinateSystem.IMAGE_PIXEL):
        raise ValidationError("coordinateSystem must be video_pixel or image_pixel")
    if fps is not None and (
        isinstance(fps, bool) or not isinstance(fps, (int, float)) or not 0 < fps <= 1000
    ):
        raise ValidationError("fps must be a positive number")

    project = getattr(patient, "project", None)
    if project is None:
        raise ValidationError("this patient belongs to no project, so it has no labels")
    schema = project_label_schema(project)
    known = set(schema.definitions.values_list("code", flat=True))
    decoded = _decode_frames(frames, known)

    # A stale save is refused before any bytes move. The authoritative check is still
    # ``record_revision``'s unique constraint, inside the transaction.
    existing = _find_set(patient)
    if expected_revision is not None:
        current = current_revision_number(existing) if existing else 0
        if current != expected_revision:
            raise AnnotationConflict(
                f"revision {current} exists, not {expected_revision}; reload and reapply"
            )

    uploaded = []
    try:
        prepared = _store_frames(patient, source_file, decoded, uploaded)
        return _write_revision(
            patient,
            source_file,
            prepared,
            schema,
            author=author,
            expected_revision=expected_revision,
            coordinate_system=coordinate_system,
            fps=fps,
        )
    except Exception:
        _discard(uploaded)
        raise
