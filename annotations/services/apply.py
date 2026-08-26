"""Applying adapter descriptors: the bridge between pure translation and writes.

An adapter says *what* to write; this says *where*. It resolves the label code a
descriptor names to a ``LabelDefinition`` in the set's own schema, reuses or
creates the selector a descriptor asks for, and dispatches to the item service
that validates and saves.

Two behaviours are worth being explicit about.

**An unknown descriptor kind is an error, not a skip.** A conversion that
silently ignored a descriptor would report success while dropping data, and the
only place that would show up is a patient's screen months later.

**An unresolvable label code is an error too**, unless the caller opts out. A
tooth polygon whose FDI code has no definition in the schema is a polygon that
will be drawn in the wrong colour and exported under the wrong segment number;
writing it unlabelled is worse than refusing, because it looks fine.
"""

from django.core.exceptions import ValidationError

from annotations.adapters import descriptors as d
from annotations.models import AnnotationSelector, LabelDefinition
from annotations.services import items as item_services


def _resolve_label(annotation_set, code, *, require):
    if code is None:
        return None
    schema_id = annotation_set.label_schema_id
    if schema_id is None:
        if require:
            raise ValidationError(
                f"descriptor names label {code!r} but the set declares no schema"
            )
        return None
    label = LabelDefinition.objects.filter(schema_id=schema_id, code=str(code)).first()
    if label is None and require:
        raise ValidationError(
            f"label code {code!r} is not defined in schema {schema_id}; writing the "
            "item unlabelled would put it under the wrong segment on export"
        )
    return label


def _resolve_selector(target, spec, cache):
    """Reuse a selector across the descriptors that share one.

    Every stroke on one video frame names the same instant, and every arch
    control point the same slice. Creating a row per descriptor would leave
    hundreds of identical selectors and make "which annotations are on this
    frame" a scan instead of a lookup.
    """
    if not spec:
        return None
    key = (
        target.pk,
        spec.get("kind"),
        spec.get("coordinate_system"),
        spec.get("frame_index"),
        spec.get("slice_axis", ""),
        spec.get("slice_index"),
        spec.get("start_time_ms"),
        spec.get("end_time_ms"),
        spec.get("segment_value"),
    )
    if key in cache:
        return cache[key]

    selector, _ = AnnotationSelector.objects.get_or_create(
        target=target,
        kind=spec["kind"],
        coordinate_system=spec["coordinate_system"],
        frame_index=spec.get("frame_index"),
        slice_axis=spec.get("slice_axis", ""),
        slice_index=spec.get("slice_index"),
        start_time_ms=spec.get("start_time_ms"),
        end_time_ms=spec.get("end_time_ms"),
        segment_value=spec.get("segment_value"),
        defaults={"bounds": spec.get("bounds") or {}},
    )
    cache[key] = selector
    return selector


def apply_descriptors(revision, target, descriptor_list, *, require_labels=True):
    """Write every descriptor against ``revision``/``target``. Returns the items.

    Runs inside whatever transaction the caller opened -- and callers should
    open one, so a conversion that fails halfway leaves no half-converted set
    behind.
    """
    annotation_set = revision.annotation_set
    selector_cache = {}
    written = []

    for index, descriptor in enumerate(descriptor_list):
        kind = descriptor.get("item")
        if kind not in d.ITEM_KINDS:
            raise ValidationError(f"descriptor {index} has unknown item kind {kind!r}")

        label = _resolve_label(
            annotation_set, descriptor.get("label_code"), require=require_labels
        )
        selector = _resolve_selector(target, descriptor.get("selector"), selector_cache)
        shared = {
            "selector": selector,
            "label": label,
            "order": descriptor.get("order", 0),
            "attributes": descriptor.get("attributes") or {},
        }

        if kind == d.GEOMETRY_2D:
            written.append(
                item_services.add_geometry_2d(
                    revision,
                    target,
                    geometry_type=descriptor["geometry_type"],
                    coordinate_system=descriptor["coordinate_system"],
                    points=descriptor["points"],
                    closed=descriptor.get("closed", False),
                    stroke_width=descriptor.get("stroke_width"),
                    **shared,
                )
            )
        elif kind == d.SPATIAL_3D:
            written.append(
                item_services.add_spatial_3d(
                    revision,
                    target,
                    geometry_type=descriptor["geometry_type"],
                    coordinate_system=descriptor["coordinate_system"],
                    points=descriptor["points"],
                    frame_of_reference_uid=descriptor.get("frame_of_reference_uid", ""),
                    **shared,
                )
            )
        elif kind == d.MEASUREMENT:
            written.append(
                item_services.add_measurement(
                    revision,
                    target,
                    kind=descriptor["kind"],
                    value=descriptor["value"],
                    unit=descriptor["unit"],
                    is_calibrated=descriptor.get("is_calibrated", False),
                    calibration_note=descriptor.get("calibration_note", ""),
                    sample_count=descriptor.get("sample_count"),
                    **shared,
                )
            )
        elif kind == d.TEMPORAL:
            written.append(
                item_services.add_temporal(
                    revision,
                    target,
                    start_time_ms=descriptor["start_time_ms"],
                    end_time_ms=descriptor["end_time_ms"],
                    **shared,
                )
            )
        elif kind == d.EVENT:
            written.append(
                item_services.add_event(
                    revision,
                    target,
                    event_type=descriptor["event_type"],
                    value=descriptor.get("value", ""),
                    time_ms=descriptor.get("time_ms"),
                    **shared,
                )
            )

    return written
