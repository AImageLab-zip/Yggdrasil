"""The annotations themselves: geometry, measurements, intervals and events.

Four item types instead of one polymorphic table, because their invariants are
genuinely disjoint. A 2D polygon needs a frame or slice selector and two
ordinates per point; a 3D landmark needs a three-space frame and may carry a
Frame of Reference UID; an interval needs two millisecond stamps and no
geometry at all. Collapsing them would mean every column nullable, every rule in
Python, and nothing the database could refuse.

Items hang off a revision, not off the set. A revision is a complete snapshot
(decision #14: labelmap editing is destructive, so there are no strokes to
replay), which means reading a set at revision *n* is one filtered query and
never a fold over deltas.
"""

from django.db import models

from annotations.constants import (
    CoordinateSystem,
    Geometry2DType,
    Geometry3DType,
    MeasurementKind,
    MeasurementUnit,
)
from annotations.models.labels import LabelDefinition
from annotations.models.sets import (
    AnnotationRevision,
    AnnotationSelector,
    AnnotationTarget,
)


class AnnotationItemBase(models.Model):
    """Fields every item shares: which revision, which target, which label."""

    revision = models.ForeignKey(
        AnnotationRevision, on_delete=models.CASCADE, related_name="%(class)ss"
    )
    target = models.ForeignKey(
        AnnotationTarget,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="%(class)ss",
        help_text=(
            "The resource this item is anchored to. NULL means the set as a "
            "whole: an occlusion classification or a voice caption is a "
            "statement about the study, and a patient may have no file for it "
            "to point at. Geometry and measurements always require one -- see "
            "annotations.services.items."
        ),
    )
    selector = models.ForeignKey(
        AnnotationSelector,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="%(class)ss",
        help_text="Narrows the target (a frame, a slice). NULL applies to the whole target.",
    )
    label = models.ForeignKey(
        LabelDefinition,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="%(class)ss",
        help_text="PROTECT: retiring a label must not silently unlabel existing work.",
    )
    order = models.PositiveIntegerField(
        default=0, help_text="Display order within the revision; not semantic."
    )
    attributes = models.JSONField(
        default=dict,
        blank=True,
        help_text="Tool-specific extras. Never a place for coordinates or labels.",
    )

    class Meta:
        abstract = True


class Geometry2DItem(AnnotationItemBase):
    """A planar shape: tooth polygons, video strokes, arch control points.

    ``points`` is ``[[x, y], ...]`` in ``coordinate_system``. The frame is on the
    row rather than inherited from the selector because the two can legitimately
    differ -- a video's SAM2 prompt points are normalized fractions while the
    stroke drawn from them is in frame pixels, and both belong to the same
    selector.
    """

    geometry_type = models.CharField(max_length=20, choices=Geometry2DType.CHOICES)
    coordinate_system = models.CharField(
        max_length=32,
        choices=CoordinateSystem.CHOICES,
        help_text="Must be one of CoordinateSystem.TWO_D; validated on write.",
    )
    points = models.JSONField(
        default=list, help_text="[[x, y], ...]. Point counts are validated per geometry_type."
    )
    closed = models.BooleanField(
        default=False,
        help_text="Whether the last point joins the first. Always true for a polygon.",
    )
    stroke_width = models.FloatField(
        null=True,
        blank=True,
        help_text=(
            "Brush width in the row's own coordinate system, for stroke tools. "
            "Part of the geometry, not styling: it is what the mask was painted with."
        ),
    )

    class Meta:
        db_table = "annotations_geometry_2d_item"
        ordering = ["revision_id", "order", "id"]
        indexes = [
            models.Index(fields=["revision", "geometry_type"]),
            models.Index(fields=["target"]),
            models.Index(fields=["label"]),
        ]

    def __str__(self):
        return f"Geometry2D {self.pk} {self.geometry_type} [{self.coordinate_system}]"


class SpatialAnnotation3DItem(AnnotationItemBase):
    """A shape in a three-space frame: IOS landmarks, volume probes, planes.

    Kept separate from :class:`Geometry2DItem` rather than sharing a table. The
    two carry different guarantees and mixing them would let a ``rectangle``
    into a patient-space row, or a two-ordinate point into a volume.

    ``frame_of_reference_uid`` is what makes ``patient_lps_mm`` coordinates
    comparable *between* resources. Without it, two series' patient coordinates
    are two unrelated frames that happen to share axis names, and overlaying
    them is a coincidence rather than a registration.
    """

    geometry_type = models.CharField(max_length=20, choices=Geometry3DType.CHOICES)
    coordinate_system = models.CharField(
        max_length=32,
        choices=CoordinateSystem.CHOICES,
        help_text="Must be one of CoordinateSystem.THREE_D; validated on write.",
    )
    points = models.JSONField(
        default=list, help_text="[[x, y, z], ...]. Point counts are validated per geometry_type."
    )
    frame_of_reference_uid = models.CharField(
        max_length=64,
        blank=True,
        help_text=(
            "Required for cross-resource comparability of patient-space "
            "coordinates. Blank is correct for resource_local, which is scoped "
            "to one mesh and has no patient frame at all."
        ),
    )

    class Meta:
        db_table = "annotations_spatial_3d_item"
        ordering = ["revision_id", "order", "id"]
        indexes = [
            models.Index(fields=["revision", "geometry_type"]),
            models.Index(fields=["target"]),
            models.Index(fields=["label"]),
            models.Index(fields=["frame_of_reference_uid"]),
        ]

    def __str__(self):
        return f"Spatial3D {self.pk} {self.geometry_type} [{self.coordinate_system}]"


class MeasurementItem(AnnotationItemBase):
    """A number derived from geometry, with the units it is actually in.

    ``is_calibrated`` is the point of this model. A length measured on an
    intraoral photograph with no known pixel spacing is a number of *pixels*;
    reporting it as millimetres invents a physical claim the image does not
    support, and a clinician reading it has no way to tell. Phase 4 makes the
    same commitment on the viewer side by refusing to fabricate a 1 mm/px
    spacing. The check constraint below is that rule in DDL: a millimetre unit
    requires a calibrated measurement, in the database, on every write path.

    Both geometry FKs are nullable and both may be null at once: a whole-slice
    mean HU is a real measurement with no shape attached.
    """

    geometry_2d_item = models.ForeignKey(
        Geometry2DItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="measurements",
        help_text="The planar shape this number was computed from, if any.",
    )
    spatial_3d_item = models.ForeignKey(
        SpatialAnnotation3DItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="measurements",
        help_text="The three-space shape this number was computed from, if any.",
    )
    kind = models.CharField(max_length=20, choices=MeasurementKind.CHOICES)
    value = models.FloatField()
    unit = models.CharField(max_length=8, choices=MeasurementUnit.CHOICES)
    is_calibrated = models.BooleanField(
        default=False,
        help_text=(
            "True only when a real-world scale was known for the source pixels. "
            "False forbids a millimetre unit -- see the check constraint."
        ),
    )
    calibration_note = models.CharField(
        max_length=255,
        blank=True,
        help_text="Where the scale came from: a NIfTI affine, a manual ruler.",
    )
    sample_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Voxels or pixels behind a statistic; NULL for a geometric measure.",
    )

    class Meta:
        db_table = "annotations_measurement_item"
        ordering = ["revision_id", "order", "id"]
        constraints = [
            # The rule the model exists for. Angles, counts and pixel units make
            # no physical claim, so they stay legal uncalibrated; mm/mm2/mm3 do,
            # and are refused.
            models.CheckConstraint(
                condition=(
                    models.Q(is_calibrated=True)
                    | ~models.Q(unit__in=sorted(MeasurementUnit.REQUIRES_CALIBRATION))
                ),
                name="annotations_measurement_mm_requires_calibration",
            ),
        ]
        indexes = [
            models.Index(fields=["revision", "kind"]),
            models.Index(fields=["geometry_2d_item"]),
            models.Index(fields=["spatial_3d_item"]),
        ]

    def __str__(self):
        return f"Measurement {self.pk} {self.kind}={self.value}{self.unit}"


class TemporalAnnotationItem(AnnotationItemBase):
    """A labelled span of a video, in integer milliseconds.

    Half-open ``[start, end)``, so adjacent spans tile without overlapping and
    an instant is ``start == end``. Integers, not the float seconds the
    laparoscopy table currently uses: a float frame time cannot be compared for
    equality, cannot be a key, and rounds differently on every code path that
    touches it.
    """

    start_time_ms = models.PositiveIntegerField()
    end_time_ms = models.PositiveIntegerField(
        help_text="Exclusive. Equal to start_time_ms for an instant."
    )

    class Meta:
        db_table = "annotations_temporal_item"
        ordering = ["revision_id", "start_time_ms", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_time_ms__gte=models.F("start_time_ms")),
                name="annotations_temporal_span_ordered",
            ),
        ]
        indexes = [
            models.Index(fields=["revision", "start_time_ms"]),
            models.Index(fields=["target", "start_time_ms"]),
        ]

    def __str__(self):
        return f"Temporal {self.pk} [{self.start_time_ms}, {self.end_time_ms})"


class EventAnnotationItem(AnnotationItemBase):
    """A categorical statement, optionally stamped at a point in time.

    This is where annotations that are not shapes live: an occlusion class per
    facet, a quadrant marker at a timestamp, a caption attached to a study. One
    row is one statement -- ``event_type`` names what is being asserted about
    and ``label``/``value`` carry the assertion -- so five occlusion facets are
    five rows rather than five columns, and adding a sixth needs no migration.

    ``label`` is preferred over ``value`` wherever a schema exists: a
    ``LabelDefinition`` FK is a controlled vocabulary the database can enforce,
    while a free string is one typo away from a second category.
    """

    event_type = models.CharField(
        max_length=60,
        help_text="What is being asserted about, e.g. 'occlusion.sagittal_left'.",
    )
    value = models.TextField(
        blank=True,
        help_text="Free-text assertion, for statements no schema covers yet.",
    )
    time_ms = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="When it applies, for time-stamped events; NULL for a whole-study statement.",
    )

    class Meta:
        db_table = "annotations_event_item"
        ordering = ["revision_id", "order", "id"]
        indexes = [
            models.Index(fields=["revision", "event_type"]),
            models.Index(fields=["target", "time_ms"]),
            models.Index(fields=["label"]),
        ]

    def __str__(self):
        return f"Event {self.pk} {self.event_type}={self.label_id or self.value}"
