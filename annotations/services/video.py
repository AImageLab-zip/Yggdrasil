"""Laparoscopy video annotations: a labelmap per annotated frame, plus the sparse rest.

Roadmap Phase 10. The shape here differs from every earlier surface, and the difference
is decision #14: **labelmap editing is destructive, and only the mask is canonical.**
Brush and eraser mutate pixels; the revision chain is the audit trail, not a stroke log
that can be replayed. So this module does not store strokes at all.

What it stores, and why each part is where it is:

**The masks are one ``npz_mask`` artifact per revision.** The governing rule already
says dense segmentation is a file in object storage and never JSON voxels; a surgical
video has hundreds of annotated frames and regions may *overlap*, so the honest
representation is the layered binary stack the NPZ export has always written -- one
``(height, width)`` plane per region per annotated frame. A single-valued labelmap
cannot express overlap and would silently drop whichever region lost.

**Keyed by milliseconds, not by frame index.** The frame rate is a property of the video
file, so a record keyed by frame number is only readable next to the decoder that
produced it, and re-encoding a video at another rate would move every mask. The legacy
adapter already made this call for the same reason; the export converts to a frame index
at the moment it knows the fps, through ``laparoscopy.mask_raster``.

**Region identity is the label code, not an array index.** The NPZ's class axis is the
project's ``RegionType`` rows in order, so adding a region type shifts every axis after
it. Storing by axis would silently re-label historical work the first time somebody adds
a category.

**SAM2 prompt points stay sparse rows.** A prompt is not a mask -- it is the input that
produced one, in normalised coordinates, and rasterising it would destroy it. They are
``Geometry2DItem`` points, exactly as ``annotations_convert_legacy`` writes them, so the
converted corpus and live work read back the same.

**Quadrant markers keep their own set.** ``video_quadrants`` is a separate
``AnnotationSet.kind`` with ``EventAnnotationItem`` rows, unchanged from what the
converter produces: a quadrant marker is a timeline event and has nothing to do with a
region mask, and merging them would put a revision of one under the lock of the other.
"""

import hashlib
import io
import json

import numpy as np
from django.core.exceptions import ValidationError
from django.db import transaction

from annotations.adapters import descriptors
from annotations.constants import (
    AnnotationOrigin,
    CoordinateSystem,
    Geometry2DType,
    PayloadFormat,
    ResourceKind,
)
from annotations.services.sets import add_payload
from annotations.services.viewer import save_measurement_groups

#: The set kinds, byte-identical to what ``annotations_convert_legacy`` writes. Reusing
#: them is what lets a converted study and a live save land in one set rather than two.
REGIONS_KIND = "video_regions"
QUADRANTS_KIND = "video_quadrants"

#: The role the video file is anchored under. Same reason as the panoramic's:
#: ``attach_target`` keys on ``(set, resource, role)``, so a live save under a different
#: role would give a converted study two targets and read back twice.
VIDEO_ROLE = ""

#: A save with more annotated frames than this is refused rather than written. A long
#: operation has hundreds of annotated frames, not tens of thousands; the guard is
#: against a client resending its buffer in a loop, the same reason
#: ``MAX_ANNOTATIONS_PER_REVISION`` exists.
MAX_ANNOTATED_FRAMES = 5000


class VideoMaskError(ValidationError):
    """A mask payload that cannot be decoded into the frame it claims to describe."""


# ------------------------------------------------------------------- the vocabulary


def region_label_schema(project):
    """The label schema mirroring one project's ``RegionType`` rows.

    Region types are a per-project, user-defined vocabulary, so unlike FDI they cannot be
    seeded in a migration -- the schema is created on demand and kept in step here. This
    lives in the service layer rather than in the conversion command that first needed it
    because the live save needs the *same* schema: a converted study and an edited one
    that resolved their region names against two different schemas would compare as a
    gap on every field.
    """
    from annotations.models import LabelDefinition, LabelSchema
    from laparoscopy.models import RegionType

    schema, _created = LabelSchema.objects.get_or_create(
        slug=f"laparoscopy-regions-project-{project.pk}",
        version=1,
        defaults={
            "name": f"Laparoscopy regions ({project.name})",
            "domain": "laparoscopy",
            "description": "Generated from laparoscopy.RegionType for this project.",
        },
    )
    for index, region_type in enumerate(
        RegionType.objects.filter(project=project).order_by("order", "name"), start=1
    ):
        LabelDefinition.objects.get_or_create(
            schema=schema,
            code=region_type.name,
            defaults={
                "value": index,
                "display_name": region_type.name,
                "color": region_type.color,
                "order": region_type.order,
            },
        )
    return schema


# --------------------------------------------------------------------- run lengths


def decode_rle(runs, width, height):
    """Row-major run-length pairs into an ``(height, width)`` uint8 mask.

    The wire format is a flat list of run lengths **starting with a run of zeros**, so
    an empty mask is ``[width * height]`` and a full one is ``[0, width * height]``.
    Chosen over sending raw bytes because a 1080p frame is two megabytes raw and a
    surgical mask is overwhelmingly one colour; chosen over PNG because a mask is not an
    image and round-tripping it through a codec invites a lossy setting.

    Validated rather than trusted: a run list whose total is not exactly ``width *
    height`` describes a different frame than the one it is being stored against, and
    silently padding it would put the mask on the wrong pixels.
    """
    if not isinstance(runs, (list, tuple)):
        raise VideoMaskError("a mask must be a list of run lengths")
    expected = int(width) * int(height)
    flat = np.zeros(expected, dtype=np.uint8)
    position = 0
    value = 0
    for index, run in enumerate(runs):
        if isinstance(run, bool) or not isinstance(run, int) or run < 0:
            raise VideoMaskError(f"run {index} must be a non-negative integer")
        end = position + run
        if end > expected:
            raise VideoMaskError(
                f"run lengths overflow a {width}x{height} frame at run {index}"
            )
        if value:
            flat[position:end] = 1
        position = end
        value ^= 1
    if position != expected:
        raise VideoMaskError(
            f"run lengths cover {position} of {expected} pixels; the mask does not "
            "describe this frame"
        )
    return flat.reshape(int(height), int(width))


def encode_rle(mask):
    """The inverse of :func:`decode_rle`, for the state endpoint."""
    flat = (np.asarray(mask, dtype=np.uint8) > 0).astype(np.uint8).reshape(-1)
    if flat.size == 0:
        return []
    change_points = np.flatnonzero(np.diff(flat)) + 1
    boundaries = np.concatenate(([0], change_points, [flat.size]))
    runs = np.diff(boundaries).tolist()
    if flat[0]:
        # The format always opens with a run of zeros, so a mask that starts filled
        # needs an explicit empty one rather than an inverted reading.
        return [0, *runs]
    return runs


# ------------------------------------------------------------------- the npz artifact


def _mask_key(time_ms, region_code):
    return f"m_{int(time_ms)}_{region_code}"


def build_mask_archive(*, width, height, frames):
    """The canonical ``.npz`` bytes for one revision.

    ``frames`` is ``{time_ms: {region_code: (height, width) array}}``. Only non-empty
    masks are written: an all-zero plane is the absence of an annotation, and storing
    one per region per frame would multiply the artifact by the size of the project's
    vocabulary for no information.

    The class vocabulary travels inside the archive rather than beside it, so the file
    is self-describing -- a mask array whose region codes have to be looked up in a
    database that has since gained a region type is not readable on its own.
    """
    payload = {
        "shape": np.asarray([int(height), int(width)], dtype=np.int64),
        "times_ms": np.asarray(sorted(frames), dtype=np.int64),
    }
    codes = set()
    for time_ms, regions in frames.items():
        for region_code, mask in regions.items():
            array = (np.asarray(mask, dtype=np.uint8) > 0).astype(np.uint8)
            if not array.any():
                continue
            codes.add(region_code)
            payload[_mask_key(time_ms, region_code)] = array
    payload["region_codes"] = np.asarray(json.dumps(sorted(codes)))

    buffer = io.BytesIO()
    np.savez_compressed(buffer, **payload)
    return buffer.getvalue()


def read_mask_archive(payload_bytes):
    """``(width, height, {time_ms: {region_code: array}})`` out of the stored bytes."""
    with np.load(io.BytesIO(payload_bytes), allow_pickle=False) as archive:
        height, width = (int(v) for v in archive["shape"])
        times = [int(v) for v in archive["times_ms"]]
        codes = json.loads(str(archive["region_codes"]))
        frames = {}
        for time_ms in times:
            regions = {}
            for code in codes:
                key = _mask_key(time_ms, code)
                if key in archive.files:
                    regions[code] = np.asarray(archive[key], dtype=np.uint8)
            frames[time_ms] = regions
    return width, height, frames


def _store_archive(patient, *, content, revision):
    """Put the archive in object storage and register the row that addresses it.

    One row per revision, not one per patient. Revisions are immutable, so a row that
    was overwritten in place would leave every earlier revision pointing at bytes that
    are no longer the ones it recorded -- which is precisely the failure
    ``SourceResource.content_hash`` exists to detect elsewhere.
    """
    from common.models import FileRegistry
    from common.object_storage import get_object_storage

    digest = hashlib.sha256(content).hexdigest()
    key = (
        f"annotations/video_masks/patient_{patient.patient_id}/"
        f"revision_{revision.revision_number}_{digest[:12]}.npz"
    )
    storage = get_object_storage()
    storage.upload_fileobj(
        io.BytesIO(content), key=key, content_type="application/octet-stream"
    )
    row, _created = FileRegistry.objects.get_or_create(
        file_path=key,
        defaults={
            "domain": "laparoscopy",
            "laparoscopy_patient": patient,
            "file_type": "annotation_mask",
            "file_size": len(content),
            "file_hash": digest,
            "metadata": {"revision": revision.revision_number, "kind": REGIONS_KIND},
        },
    )
    return row, digest


# --------------------------------------------------------------------------- saving


def _prompt_descriptors(prompts):
    """SAM2 prompt points, in the shape ``legacy_laparoscopy`` already produces.

    Kept identical on purpose: ``annotations_crosscheck`` compares a converted row
    against what a live save writes, and the prompts are the only part of a region
    annotation that survives the move to labelmaps unchanged.
    """
    out = []
    for index, prompt in enumerate(prompts or []):
        if not isinstance(prompt, dict):
            raise ValidationError(f"prompt {index} must be an object")
        try:
            x = float(prompt["x"])
            y = float(prompt["y"])
            time_ms = int(prompt["timeMs"])
        except (KeyError, TypeError, ValueError):
            raise ValidationError(f"prompt {index} needs numeric x, y and timeMs")
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValidationError(
                f"prompt {index} coordinates are normalised fractions, not pixels"
            )
        if time_ms < 0:
            raise ValidationError(f"prompt {index} has a negative time")
        code = prompt.get("regionCode") or None
        out.append(
            descriptors.geometry_2d(
                geometry_type=Geometry2DType.POINT,
                coordinate_system=CoordinateSystem.VIDEO_NORMALIZED,
                points=[[x, y]],
                selector=descriptors.interval_selector(
                    start_time_ms=time_ms,
                    end_time_ms=time_ms,
                    coordinate_system=CoordinateSystem.VIDEO_PIXEL,
                ),
                label_code=code,
                order=index,
                attributes={
                    "prompt": True,
                    # 1 is a positive prompt and 0 a negative one; SAM2 needs both, and
                    # losing the distinction inverts half of them.
                    "prompt_label": 0 if prompt.get("label") == 0 else 1,
                    "index": index,
                    **({"region_name": code} if code else {}),
                },
            )
        )
    return out


@transaction.atomic
def save_video_regions(
    patient,
    *,
    video_file,
    width,
    height,
    frames,
    prompts=(),
    author=None,
    expected_revision=None,
    origin=AnnotationOrigin.MANUAL,
    label_schema=None,
):
    """Write one revision: the labelmap archive, plus the prompts that produced it.

    :param frames: ``[{"timeMs": int, "regions": {code: {"rle": [...]}}}]`` -- the whole
        state of the work, not a delta. The client owns the entire set, which is why
        ``carry_forward`` is off: there is one video and every save names it, so a
        carried-forward item would be a region the user had just erased coming back.
    :param expected_revision: the revision the client read. A stale one is a 409 through
        ``record_revision``'s unique constraint, with no read-then-write window.
    :returns: the new ``AnnotationRevision``.
    """
    if width <= 0 or height <= 0:
        raise ValidationError("a video frame has positive dimensions")
    frames = list(frames or [])
    if len(frames) > MAX_ANNOTATED_FRAMES:
        raise ValidationError(
            f"{len(frames)} annotated frames exceeds the {MAX_ANNOTATED_FRAMES} a save "
            "may carry; this is a client resending its buffer, not a real session"
        )

    decoded = {}
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise ValidationError(f"frames[{index}] must be an object")
        try:
            time_ms = int(frame["timeMs"])
        except (KeyError, TypeError, ValueError):
            raise ValidationError(f"frames[{index}] needs an integer timeMs")
        if time_ms < 0:
            raise ValidationError(f"frames[{index}] has a negative time")
        regions = frame.get("regions") or {}
        if not isinstance(regions, dict):
            raise ValidationError(f"frames[{index}].regions must be an object")
        decoded[time_ms] = {
            code: decode_rle((entry or {}).get("rle"), width, height)
            for code, entry in regions.items()
        }

    revision = save_measurement_groups(
        patient,
        groups=[
            {
                "file_obj": video_file,
                "annotations": list(prompts or []),
                "resource_kind": ResourceKind.FILE,
                "role": VIDEO_ROLE,
                "descriptor": {"width": int(width), "height": int(height)},
            }
        ],
        author=author,
        expected_revision=expected_revision,
        coordinate_system=CoordinateSystem.VIDEO_PIXEL,
        kind=REGIONS_KIND,
        # One video, one target, every save names it: the writer owns the whole set.
        carry_forward=False,
        # The masks are the resumable state, and they are canonical. A second,
        # non-canonical copy of viewer state would be a thing to keep in step with the
        # only thing that is true.
        store_payload=False,
        translate=lambda group: _prompt_descriptors(group["annotations"]),
        origin=origin,
        label_schema=label_schema,
        note=f"video regions: {len(decoded)} annotated frames",
    )

    content = build_mask_archive(width=width, height=height, frames=decoded)
    row, digest = _store_archive(patient, content=content, revision=revision)
    add_payload(
        revision,
        format=PayloadFormat.NPZ_MASK,
        file_obj=row,
        canonical=True,
        content_hash=digest,
        byte_size=len(content),
    )
    return revision


@transaction.atomic
def save_quadrant_markers(
    patient, *, video_file, markers, author=None, expected_revision=None
):
    """Write one revision of the timeline's quadrant markers.

    Its own set, and its own revision chain: a marker is a timeline event, not a region,
    and filing them together would put an edit of one under the lock of the other.
    """
    return save_measurement_groups(
        patient,
        groups=[
            {
                "file_obj": video_file,
                "annotations": list(markers or []),
                "resource_kind": ResourceKind.FILE,
                "role": VIDEO_ROLE,
            }
        ],
        author=author,
        expected_revision=expected_revision,
        coordinate_system=CoordinateSystem.VIDEO_PIXEL,
        kind=QUADRANTS_KIND,
        carry_forward=False,
        store_payload=False,
        translate=lambda group: _marker_descriptors(group["annotations"]),
    )


def _marker_descriptors(markers):
    """Quadrant markers, in the shape ``legacy_laparoscopy.quadrant_marker`` produces."""
    out = []
    seen = set()
    for index, marker in enumerate(markers or []):
        if not isinstance(marker, dict):
            raise ValidationError(f"markers[{index}] must be an object")
        try:
            time_ms = int(marker["timeMs"])
        except (KeyError, TypeError, ValueError):
            raise ValidationError(f"markers[{index}] needs an integer timeMs")
        if time_ms < 0:
            raise ValidationError(f"markers[{index}] has a negative time")
        if time_ms in seen:
            # The legacy table had UniqueConstraint(patient, time_ms) and the timeline
            # relies on it: two classifications of one instant is not a state the UI can
            # draw, and picking one silently would discard the user's later choice.
            raise ValidationError(f"two markers claim {time_ms}ms")
        seen.add(time_ms)
        name = marker.get("quadrantName") or ""
        out.append(
            descriptors.event(
                event_type="quadrant",
                value=name,
                time_ms=time_ms,
                label_code=name or None,
                attributes={"quadrant": name} if name else {},
            )
        )
    return out


# -------------------------------------------------------------------------- reading


def _latest_revision(patient, kind):
    from annotations.models import AnnotationSet

    annotation_set = AnnotationSet.objects.filter(
        domain="laparoscopy", laparoscopy_patient=patient, kind=kind
    ).first()
    if annotation_set is None:
        return None, None
    return annotation_set, annotation_set.revisions.order_by("-revision_number").first()


def video_regions_state(patient):
    """What the editor should draw, and the revision it must quote to save.

    One endpoint for both, like every other surface: a viewer opens a study, draws what
    is there, and needs to know what to put in ``expectedRevision`` before the user's
    first stroke. Guessing zero loses a 409 that did not have to happen.
    """
    from common.file_access import open_binary

    annotation_set, revision = _latest_revision(patient, REGIONS_KIND)
    empty = {
        "revision": 0,
        "width": 0,
        "height": 0,
        "frames": [],
        "prompts": [],
    }
    if revision is None:
        return empty

    payload = revision.payloads.filter(
        format=PayloadFormat.NPZ_MASK, canonical_slot=1
    ).first()
    frames = []
    width = height = 0
    if payload is not None and payload.file_id:
        handle, _info = open_binary(payload.file.file_path)
        try:
            width, height, decoded = read_mask_archive(handle.read())
        finally:
            close = getattr(handle, "close", None)
            if close is not None:
                close()
        frames = [
            {
                "timeMs": time_ms,
                "regions": {
                    code: {"rle": encode_rle(mask)}
                    for code, mask in sorted(regions.items())
                },
            }
            for time_ms, regions in sorted(decoded.items())
        ]

    prompts = []
    for item in revision.geometry2ditems.select_related("selector", "label").all():
        if not (item.attributes or {}).get("prompt"):
            continue
        point = (item.points or [[0, 0]])[0]
        prompts.append(
            {
                "timeMs": item.selector.start_time_ms if item.selector_id else 0,
                "regionCode": item.label.code if item.label_id else None,
                "x": point[0],
                "y": point[1],
                "label": (item.attributes or {}).get("prompt_label", 1),
            }
        )

    return {
        "revision": revision.revision_number,
        "width": width,
        "height": height,
        "frames": frames,
        "prompts": prompts,
    }


def quadrant_markers_state(patient):
    """The timeline's markers and the revision to quote."""
    _annotation_set, revision = _latest_revision(patient, QUADRANTS_KIND)
    if revision is None:
        return {"revision": 0, "markers": []}
    markers = [
        {
            "timeMs": item.time_ms,
            "quadrantName": item.value or "",
        }
        for item in revision.eventannotationitems.order_by("time_ms", "id")
    ]
    return {"revision": revision.revision_number, "markers": markers}
