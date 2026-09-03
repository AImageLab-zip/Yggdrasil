"""The canonical JSON document: what a revision *is*, written down.

Every revision stores one canonical payload, and for everything except dense
voxels that payload is this document. It exists so a revision has a single
self-contained representation that does not require joining five tables to read
-- for export, for an API response, and for the cross-check that compares a
converted revision against the legacy row it came from.

The document is **domain-oriented**, not viewer-oriented. That is the governing
architectural rule made concrete, and it has one hard consequence worth stating
plainly: no Cornerstone runtime identifier may appear in it. ``annotationUID``,
``imageId``, ``volumeId``, ``segmentationId`` and ``cachedStats`` are all
session-scoped -- they encode a URL or a cache key, they change when a route
changes, and they are meaningless in the next tab. A document carrying one would
look durable and would not be. :func:`build_document` cannot emit them because
it reads from columns that cannot hold them, and
:func:`assert_no_viewer_identifiers` is the assertion that keeps it that way when
somebody later adds a passthrough field.

Coordinates are emitted with their frame on every item, never inherited from a
parent. Inheritance is how a mirrored landmark happens: one item moves to a
different resource, keeps the parent's frame by omission, and nothing reports an
error.
"""

from django.core.exceptions import ValidationError

#: Bumped when the document's shape changes in a way a reader must notice.
#: Stored in the payload so a future reader can refuse a document it does not
#: understand instead of silently misreading it.
DOCUMENT_VERSION = 1

#: Keys that must never appear anywhere in a canonical document. Session-scoped
#: viewer state, all of it -- see the module docstring.
FORBIDDEN_KEYS = frozenset(
    {
        "annotationUID",
        "annotationuid",
        "imageId",
        "imageid",
        "volumeId",
        "volumeid",
        "segmentationId",
        "segmentationid",
        "cachedStats",
        "cachedstats",
        "referencedImageId",
        "FrameOfReferenceUID",
        "toolName",
        "viewportId",
    }
)


def _label(label):
    if label is None:
        return None
    # The integer value *and* the code: the value is what a labelmap holds and
    # the code is what an external system matches on. A document with only one
    # of them is unreadable by half its audience.
    return {
        "schema": label.schema.slug,
        "schema_version": label.schema.version,
        "value": label.value,
        "code": label.code,
        "display_name": label.display_name,
    }


def _selector(selector):
    if selector is None:
        return None
    out = {"kind": selector.kind, "coordinate_system": selector.coordinate_system}
    for name in (
        "frame_index",
        "slice_index",
        "start_time_ms",
        "end_time_ms",
        "segment_value",
    ):
        value = getattr(selector, name)
        if value is not None:
            out[name] = value
    if selector.slice_axis:
        out["slice_axis"] = selector.slice_axis
    if selector.bounds:
        out["bounds"] = selector.bounds
    return out


def _target(target):
    if target is None:
        return None
    return {
        "identity_key": target.source_resource.identity_key,
        "kind": target.source_resource.kind,
        "role": target.role,
    }


def _common(item):
    return {
        "target": _target(item.target),
        "selector": _selector(item.selector),
        "label": _label(item.label),
        "order": item.order,
        "attributes": item.attributes or {},
    }


def build_document(revision):
    """Serialize one revision into its canonical document.

    Reads the whole item graph. The caller should have prefetched it; this does
    not, because the two useful callers -- a write that has the objects in hand,
    and a bulk export that wants to control its own query plan -- want different
    things and neither is served by a hardcoded ``prefetch_related``.
    """
    annotation_set = revision.annotation_set
    document = {
        "document_version": DOCUMENT_VERSION,
        "set": {
            "id": annotation_set.pk,
            "kind": annotation_set.kind,
            "domain": annotation_set.domain,
            "status": annotation_set.status,
            "label_schema": (
                annotation_set.label_schema.slug if annotation_set.label_schema else None
            ),
            "label_schema_version": (
                annotation_set.label_schema.version if annotation_set.label_schema else None
            ),
        },
        "revision": {
            "number": revision.revision_number,
            "origin": revision.origin,
            "note": revision.note,
            "created_at": revision.created_at.isoformat(),
            # The provenance that makes the rest of the document checkable: what
            # the targets hashed to when this was written.
            "source_fingerprint": revision.source_fingerprint or {},
        },
        "targets": [
            {
                "identity_key": target.source_resource.identity_key,
                "kind": target.source_resource.kind,
                "role": target.role,
                "primary": target.primary_slot == 1,
                "content_hash": target.source_resource.content_hash,
                "descriptor": target.source_resource.descriptor or {},
            }
            for target in annotation_set.targets.select_related("source_resource")
        ],
        "geometry_2d": [
            {
                **_common(item),
                "geometry_type": item.geometry_type,
                "coordinate_system": item.coordinate_system,
                "points": item.points,
                "closed": item.closed,
                "stroke_width": item.stroke_width,
            }
            for item in revision.geometry2ditems.all()
        ],
        "spatial_3d": [
            {
                **_common(item),
                "geometry_type": item.geometry_type,
                "coordinate_system": item.coordinate_system,
                "points": item.points,
                "frame_of_reference_uid": item.frame_of_reference_uid,
            }
            for item in revision.spatialannotation3ditems.all()
        ],
        "measurements": [
            {
                **_common(item),
                "kind": item.kind,
                "value": item.value,
                "unit": item.unit,
                # Emitted always, never omitted when false. A reader that has to
                # infer calibration from the unit will get it wrong the first
                # time somebody adds a unit to the list.
                "is_calibrated": item.is_calibrated,
                "calibration_note": item.calibration_note,
                "sample_count": item.sample_count,
            }
            for item in revision.measurementitems.all()
        ],
        "temporal": [
            {
                **_common(item),
                "start_time_ms": item.start_time_ms,
                "end_time_ms": item.end_time_ms,
            }
            for item in revision.temporalannotationitems.all()
        ],
        "events": [
            {
                **_common(item),
                "event_type": item.event_type,
                "value": item.value,
                "time_ms": item.time_ms,
            }
            for item in revision.eventannotationitems.all()
        ],
    }
    return document


def _walk(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}.{key}" if path else str(key), key, value
            yield from _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def assert_no_viewer_identifiers(document):
    """Refuse a document carrying session-scoped viewer state.

    Cheap, and worth running on every write. The failure it prevents is not a
    crash: a document with an ``annotationUID`` in it is perfectly readable, and
    stays readable right up until somebody uses that id to look something up in
    a session where it means nothing -- or exports it as though it were a
    durable identifier.
    """
    offenders = sorted(
        {path for path, key, _ in _walk(document) if key in FORBIDDEN_KEYS}
    )
    if offenders:
        raise ValidationError(
            "the canonical document must not carry viewer runtime identifiers; "
            f"found {offenders}"
        )
    return document


def document_summary(document):
    """Item counts by kind -- for logs and for the cross-check's diff output.

    A summary rather than the whole document, because a conversion over ten
    thousand patients that logged full documents would produce a log nobody
    reads.
    """
    return {
        section: len(document.get(section) or [])
        for section in ("geometry_2d", "spatial_3d", "measurements", "temporal", "events")
    }
