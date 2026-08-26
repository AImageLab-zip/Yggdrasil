"""Writing the annotations themselves, validated first, every time.

Each function here runs the pure validator for its item type and then saves what
the validator returned -- not what the caller passed. That distinction matters:
the validator normalizes ordinates to floats, so an integer pixel index written
through this layer reads back the same way it would if it had arrived as a
float, and a round-trip comparison in a test or a crosscheck is not defeated by
``2 != 2.0`` in JSON.

The cross-checks the pure validators cannot do -- does this label belong to the
set's schema, does this selector belong to this target -- are here, because they
need the database.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from annotations.constants import CoordinateSystem
from annotations.models import (
    EventAnnotationItem,
    Geometry2DItem,
    MeasurementItem,
    SpatialAnnotation3DItem,
    TemporalAnnotationItem,
)
from annotations.validators import (
    validate_geometry_2d,
    validate_geometry_3d,
    validate_item_selector_pairing,
    validate_measurement,
)


def _check_membership(revision, target, selector, label):
    """The relationships a pure validator cannot see.

    Every one of these is a way to build a row that reads back as valid and
    describes the wrong thing: an item on one set pointing at another set's
    target, a selector that narrows a resource the item is not anchored to, a
    label from a schema the set does not use.
    """
    if target.annotation_set_id != revision.annotation_set_id:
        raise ValidationError(
            "the target belongs to a different annotation set than the revision"
        )
    if selector is not None and selector.target_id != target.pk:
        raise ValidationError("the selector narrows a different target")
    if label is not None:
        schema_id = revision.annotation_set.label_schema_id
        if schema_id is None:
            raise ValidationError(
                "this set declares no label schema, so it cannot carry labels"
            )
        if label.schema_id != schema_id:
            raise ValidationError(
                "the label comes from a schema this set does not use; its integer "
                "value would mean something else here"
            )


def _pairing(coordinate_system, selector):
    validate_item_selector_pairing(
        coordinate_system=coordinate_system,
        selector_kind=selector.kind if selector else None,
        selector_axis=selector.slice_axis if selector else "",
    )


@transaction.atomic
def add_geometry_2d(
    revision,
    target,
    *,
    geometry_type,
    coordinate_system,
    points,
    closed=False,
    selector=None,
    label=None,
    stroke_width=None,
    order=0,
    attributes=None,
):
    """Write one planar shape."""
    _check_membership(revision, target, selector, label)
    _pairing(coordinate_system, selector)
    cleaned = validate_geometry_2d(
        geometry_type=geometry_type,
        coordinate_system=coordinate_system,
        points=points,
        closed=closed,
    )
    return Geometry2DItem.objects.create(
        revision=revision,
        target=target,
        selector=selector,
        label=label,
        geometry_type=geometry_type,
        coordinate_system=coordinate_system,
        points=cleaned,
        closed=closed,
        stroke_width=stroke_width,
        order=order,
        attributes=attributes or {},
    )


@transaction.atomic
def add_spatial_3d(
    revision,
    target,
    *,
    geometry_type,
    coordinate_system,
    points,
    frame_of_reference_uid="",
    selector=None,
    label=None,
    order=0,
    attributes=None,
):
    """Write one shape in a three-space frame."""
    _check_membership(revision, target, selector, label)
    attributes = attributes or {}
    cleaned = validate_geometry_3d(
        geometry_type=geometry_type,
        coordinate_system=coordinate_system,
        points=points,
        frame_of_reference_uid=frame_of_reference_uid,
        attributes=attributes,
    )
    # A mesh's object space is defined by one resource, so the coordinates are
    # only meaningful against that resource. Anchoring them to a target the
    # frame does not come from is the silent version of plotting a landmark on
    # the wrong model.
    if coordinate_system == CoordinateSystem.RESOURCE_LOCAL and not target.source_resource_id:
        raise ValidationError("resource_local coordinates need a resolved target resource")

    return SpatialAnnotation3DItem.objects.create(
        revision=revision,
        target=target,
        selector=selector,
        label=label,
        geometry_type=geometry_type,
        coordinate_system=coordinate_system,
        points=cleaned,
        frame_of_reference_uid=frame_of_reference_uid,
        order=order,
        attributes=attributes,
    )


@transaction.atomic
def add_measurement(
    revision,
    target,
    *,
    kind,
    value,
    unit,
    is_calibrated=False,
    calibration_note="",
    geometry_2d_item=None,
    spatial_3d_item=None,
    sample_count=None,
    selector=None,
    label=None,
    order=0,
    attributes=None,
):
    """Write one number, with the unit it has actually earned."""
    _check_membership(revision, target, selector, label)
    if geometry_2d_item is not None and spatial_3d_item is not None:
        raise ValidationError(
            "a measurement derives from one shape; naming two leaves it ambiguous "
            "which one the number describes"
        )
    for shape in (geometry_2d_item, spatial_3d_item):
        if shape is not None and shape.revision_id != revision.pk:
            raise ValidationError(
                "the measured shape belongs to a different revision"
            )

    cleaned = validate_measurement(
        kind=kind,
        value=value,
        unit=unit,
        is_calibrated=is_calibrated,
        sample_count=sample_count,
    )
    return MeasurementItem.objects.create(
        revision=revision,
        target=target,
        selector=selector,
        label=label,
        geometry_2d_item=geometry_2d_item,
        spatial_3d_item=spatial_3d_item,
        kind=kind,
        value=cleaned,
        unit=unit,
        is_calibrated=is_calibrated,
        calibration_note=calibration_note,
        sample_count=sample_count,
        order=order,
        attributes=attributes or {},
    )


@transaction.atomic
def add_temporal(
    revision,
    target,
    *,
    start_time_ms,
    end_time_ms,
    selector=None,
    label=None,
    order=0,
    attributes=None,
):
    """Write one labelled span of a video, half-open and in integer milliseconds."""
    _check_membership(revision, target, selector, label)
    for name, value in (("start_time_ms", start_time_ms), ("end_time_ms", end_time_ms)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(
                f"{name} must be integer milliseconds, got {value!r}; float seconds "
                "cannot be compared for equality or used as a key"
            )
        if value < 0:
            raise ValidationError(f"{name} must not be negative")
    if end_time_ms < start_time_ms:
        raise ValidationError("the span ends before it starts")

    return TemporalAnnotationItem.objects.create(
        revision=revision,
        target=target,
        selector=selector,
        label=label,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        order=order,
        attributes=attributes or {},
    )


@transaction.atomic
def add_event(
    revision,
    target,
    *,
    event_type,
    label=None,
    value="",
    time_ms=None,
    selector=None,
    order=0,
    attributes=None,
):
    """Write one categorical statement.

    A label is preferred over a free string wherever the set has a schema: a
    ``LabelDefinition`` is a controlled vocabulary the database can enforce,
    while a string is one typo away from a second category that looks like a
    real one in every report.
    """
    _check_membership(revision, target, selector, label)
    if not event_type:
        raise ValidationError("an event must say what it asserts about")
    if label is None and not value and not attributes:
        # A bare event_type asserts nothing -- "quadrant" with no quadrant named
        # is not a marker. Attributes count, because some events *are* their own
        # assertion: a voice caption whose transcription has not run yet still
        # records that somebody recorded one.
        raise ValidationError("an event needs a label, a value or attributes")
    if time_ms is not None:
        if isinstance(time_ms, bool) or not isinstance(time_ms, int) or time_ms < 0:
            raise ValidationError("time_ms must be non-negative integer milliseconds")

    return EventAnnotationItem.objects.create(
        revision=revision,
        target=target,
        selector=selector,
        label=label,
        event_type=event_type,
        value=value,
        time_ms=time_ms,
        order=order,
        attributes=attributes or {},
    )
