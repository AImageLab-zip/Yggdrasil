"""The spine: a set of annotation work, what it points at, and its revisions.

Reading order is ``AnnotationSet`` -> ``AnnotationTarget`` (+ its
``AnnotationSelector`` rows) -> ``AnnotationRevision`` -> ``AnnotationPayload``.
A set says *whose work this is and what kind*; targets say *what content it is
anchored to*; a revision is one saved state of that work; payloads are the
encodings of that state, exactly one of which is canonical.

Two invariants here are enforced by the database rather than by code, because
both fail in ways that are hard to see:

* ``UniqueConstraint(annotation_set, revision_number)`` **is** the
  optimistic-concurrency primitive. Two browsers that both loaded revision 7 and
  both save will both try to insert revision 8; one wins and the other gets an
  ``IntegrityError``, which the service turns into a 409. There is no read-then-
  write window to lose, because the check *is* the write.
* "Exactly one primary target" and "exactly one canonical payload" are nullable
  slot columns, not conditional constraints. Django compiles
  ``UniqueConstraint(condition=...)`` to nothing on MySQL -- no partial index and
  no error (F12) -- so the rule would be enforced in tests on SQLite and absent
  in production. A ``primary_slot`` that is ``1`` on the primary row and ``NULL``
  everywhere else gets the same guarantee out of plain ``UNIQUE``, because MySQL
  treats NULLs as distinct.
"""

from django.contrib.auth.models import User
from django.db import models

from annotations.constants import (
    AnnotationOrigin,
    AnnotationStatus,
    CoordinateSystem,
    PayloadFormat,
    SelectorKind,
    SliceAxis,
)
from annotations.models.labels import LabelSchema
from annotations.models.resources import SourceResource
from common.domains import DOMAIN_CHOICES
from common.models import DomainFKAccessorMixin


class AnnotationSet(DomainFKAccessorMixin, models.Model):
    """One body of annotation work on one patient.

    The three parallel patient FKs are the house pattern (``common.Job``,
    ``common.FileRegistry``); ``DomainFKAccessorMixin`` gives
    ``get_patient()``/``set_patient()`` so callers never branch on ``domain``.

    ``kind`` and ``annotation_method`` are deliberately separate. ``kind`` is
    Yggdrasil's own word for what this work *is* and is never null;
    ``annotation_method`` is the hook into the project-level on/off switch and
    is null for work the project registry does not gate. Collapsing them would
    make the domain model depend on an administrative toggle, which is the thing
    the governing architectural rule forbids.
    """

    #: Set kinds. Not a constants-module enum: unlike the vocabularies there,
    #: this list grows once per migrated surface and each entry is only ever
    #: written by one adapter.
    KIND_CHOICES = [
        ("ios_landmarks", "IOS landmarks"),
        ("intraoral_segmentation", "Intraoral tooth segmentation"),
        ("occlusion_classification", "Occlusion classification"),
        ("panoramic_arch", "Panoramic arch"),
        ("video_regions", "Video region annotation"),
        ("video_quadrants", "Video quadrant markers"),
        ("voice_caption", "Voice caption"),
        ("volume_segmentation", "Volume segmentation"),
        ("measurements", "Measurements"),
    ]

    kind = models.CharField(max_length=40, choices=KIND_CHOICES)
    domain = models.CharField(max_length=20, choices=DOMAIN_CHOICES, default="maxillo")
    patient = models.ForeignKey(
        "maxillo.Patient",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="annotation_sets",
    )
    brain_patient = models.ForeignKey(
        "brain.Patient",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="annotation_sets",
    )
    laparoscopy_patient = models.ForeignKey(
        "laparoscopy.Patient",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="annotation_sets",
    )

    annotation_method = models.ForeignKey(
        "common.AnnotationMethod",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="annotation_sets",
        help_text=(
            "The project-level gate for this work, where one exists. NULL means "
            "the registry does not gate this kind (the panoramic arch, whose "
            "editability is governed by the annotation lock instead)."
        ),
    )
    label_schema = models.ForeignKey(
        LabelSchema,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="annotation_sets",
        help_text="Required for anything with labels; NULL for pure geometry.",
    )

    status = models.CharField(
        max_length=20, choices=AnnotationStatus.CHOICES, default=AnnotationStatus.DRAFT
    )
    ever_annotated = models.BooleanField(
        default=False,
        help_text=(
            "Monotonic: set true the first time human annotation work is "
            "recorded and never cleared. Decision #18 -- deleting the work does "
            "not unfreeze the raw data it was drawn on, because the bytes were "
            "already interpreted and the record has to stay explicable."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_annotation_sets",
    )

    class Meta:
        db_table = "annotations_annotation_set"
        ordering = ["-updated_at", "id"]
        indexes = [
            models.Index(fields=["domain", "kind"]),
            models.Index(fields=["patient", "kind"]),
            models.Index(fields=["brain_patient", "kind"]),
            models.Index(fields=["laparoscopy_patient", "kind"]),
            # The raw-data lock's query: "does this patient have annotation work?"
            models.Index(fields=["domain", "ever_annotated"]),
        ]

    def __str__(self):
        return f"AnnotationSet {self.pk} {self.domain}/{self.kind}"


class AnnotationTarget(models.Model):
    """The content one set is anchored to. A set may have several.

    Several is the normal case, not an edge case: a panoramic arch is drawn
    against a volume *and* produces strips, and a fused study has two volumes.
    ``primary_slot`` names the one whose coordinate frame the set's geometry is
    expressed in by default.
    """

    annotation_set = models.ForeignKey(
        AnnotationSet, on_delete=models.CASCADE, related_name="targets"
    )
    source_resource = models.ForeignKey(
        SourceResource, on_delete=models.PROTECT, related_name="targets"
    )
    role = models.CharField(
        max_length=40,
        blank=True,
        help_text="What this target is to the set: 'volume', 'segmentation', 'image', 'video'.",
    )
    primary_slot = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        default=None,
        help_text=(
            "1 on the primary target, NULL on every other. A nullable slot, not "
            "a conditional constraint, because MySQL drops those silently (F12)."
        ),
    )
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "annotations_annotation_target"
        ordering = ["annotation_set_id", "order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["annotation_set", "primary_slot"],
                name="annotations_uniq_target_primary_slot",
            ),
            models.UniqueConstraint(
                fields=["annotation_set", "source_resource", "role"],
                name="annotations_uniq_target_resource_role",
            ),
        ]
        indexes = [
            models.Index(fields=["source_resource"]),
        ]

    def __str__(self):
        return f"Target {self.pk} -> {self.source_resource_id} ({self.role or 'unset'})"


class AnnotationSelector(models.Model):
    """Which part of a target the work applies to.

    Every selector declares a ``coordinate_system``, including the ones with no
    geometry -- ``CoordinateSystem.NONE`` is a real answer for "this applies to
    the whole file". A blank would be indistinguishable from an omission.

    Times are **integer milliseconds**, never floats. The existing laparoscopy
    tables have it both ways -- ``QuadrantClassificationMarker.time_ms`` is an
    integer, ``RegionAnnotation.frame_time`` is a float in seconds -- and the
    float is the one that cannot be compared for equality or used as a key
    without rounding rules nobody wrote down. Milliseconds are exact at every
    frame rate a video in this system will have.
    """

    target = models.ForeignKey(
        AnnotationTarget, on_delete=models.CASCADE, related_name="selectors"
    )
    kind = models.CharField(max_length=32, choices=SelectorKind.CHOICES)
    coordinate_system = models.CharField(
        max_length=32,
        choices=CoordinateSystem.CHOICES,
        help_text="Never blank. 'none' where the selection has no geometry.",
    )

    frame_index = models.PositiveIntegerField(null=True, blank=True)
    slice_axis = models.CharField(max_length=16, choices=SliceAxis.CHOICES, blank=True)
    slice_index = models.PositiveIntegerField(null=True, blank=True)
    start_time_ms = models.PositiveIntegerField(null=True, blank=True)
    end_time_ms = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Exclusive end of a half-open interval. Equal to start for an instant.",
    )
    segment_value = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Labelmap segment this selector picks out; matches LabelDefinition.value.",
    )
    bounds = models.JSONField(
        default=dict,
        blank=True,
        help_text="Axis-aligned extent in this selector's coordinate system, when kind=spatial_bounds.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "annotations_annotation_selector"
        ordering = ["target_id", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(end_time_ms__isnull=True)
                    | models.Q(start_time_ms__isnull=True)
                    | models.Q(end_time_ms__gte=models.F("start_time_ms"))
                ),
                name="annotations_selector_time_span_ordered",
            ),
        ]
        indexes = [
            models.Index(fields=["target", "kind"]),
            models.Index(fields=["target", "start_time_ms"]),
            models.Index(fields=["target", "slice_axis", "slice_index"]),
        ]

    def __str__(self):
        return f"Selector {self.pk} {self.kind} [{self.coordinate_system}]"


class AnnotationRevision(models.Model):
    """One saved state of a set. The audit trail, and the concurrency primitive.

    Revisions are snapshots, not deltas. Decision #14 makes labelmap editing
    destructive -- the brush mutates voxels and only the mask is canonical -- so
    there are no strokes to replay and a revision has to stand on its own.

    ``source_fingerprint`` records what the targets hashed to when this revision
    was written. A later mismatch does not mean the annotation is wrong, but it
    does mean nobody can claim it is right: the bytes moved underneath it. The
    F11 fix and the raw-data lock exist to keep that from happening; the
    fingerprint is how ``annotations_crosscheck`` notices when it did anyway.
    """

    annotation_set = models.ForeignKey(
        AnnotationSet, on_delete=models.CASCADE, related_name="revisions"
    )
    revision_number = models.PositiveIntegerField(
        help_text="1-based and gapless. Allocated by the write, not read beforehand."
    )
    origin = models.CharField(
        max_length=32, choices=AnnotationOrigin.CHOICES, default=AnnotationOrigin.MANUAL
    )
    author = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="annotation_revisions",
    )
    note = models.CharField(max_length=255, blank=True)
    source_fingerprint = models.JSONField(
        default=dict,
        blank=True,
        help_text="{identity_key: content_hash} for every target, as of this revision.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "annotations_annotation_revision"
        ordering = ["annotation_set_id", "-revision_number"]
        constraints = [
            # See the module docstring: this is the optimistic-concurrency
            # primitive, not a tidiness rule. Do not add a condition to it --
            # MySQL would drop the whole constraint (F12) and take 409s with it.
            models.UniqueConstraint(
                fields=["annotation_set", "revision_number"],
                name="annotations_uniq_revision_number",
            ),
        ]
        indexes = [
            models.Index(fields=["annotation_set", "created_at"]),
            models.Index(fields=["author"]),
        ]

    def __str__(self):
        return f"Revision {self.annotation_set_id}#{self.revision_number}"


class AnnotationPayload(models.Model):
    """One encoding of a revision's content. A revision may have several.

    One-to-many rather than one-to-one because the same work legitimately exists
    in more than one form at once: the canonical NIfTI labelmap, a DICOM SEG for
    export, a PNG render for the panoramic strips, and a Cornerstone scratch
    state so the next editing session resumes where the last one stopped. Only
    the canonical one is read back as truth.

    Bytes never live in this table. An artifact-format payload points at a
    ``FileRegistry`` row; an inline format keeps its JSON in ``data``. Dense
    voxels are always the former -- storing a labelmap as JSON would be both
    enormous and lossy about dtype.
    """

    revision = models.ForeignKey(
        AnnotationRevision, on_delete=models.CASCADE, related_name="payloads"
    )
    format = models.CharField(max_length=32, choices=PayloadFormat.CHOICES)
    canonical_slot = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        default=None,
        help_text=(
            "1 on the canonical payload, NULL on every other. Nullable slot for "
            "the same reason as AnnotationTarget.primary_slot (F12)."
        ),
    )
    variant = models.CharField(
        max_length=40,
        blank=True,
        help_text=(
            "Distinguishes two payloads of the same format on one revision. The "
            "panoramic bakes both a MIP and a ray-sum strip, so 'one png_render "
            "per revision' would be wrong; blank where a format occurs once."
        ),
    )
    data = models.JSONField(
        null=True,
        blank=True,
        help_text="Inline content for JSON formats; NULL for artifact formats.",
    )
    file = models.ForeignKey(
        "common.FileRegistry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="annotation_payloads",
        help_text="Artifact bytes in object storage; NULL for inline formats.",
    )
    content_hash = models.CharField(max_length=64, blank=True)
    byte_size = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "annotations_annotation_payload"
        ordering = ["revision_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["revision", "canonical_slot"],
                name="annotations_uniq_payload_canonical_slot",
            ),
            models.UniqueConstraint(
                fields=["revision", "format", "variant"],
                name="annotations_uniq_payload_format_variant",
            ),
            # A payload that is neither inline nor an artifact is a row that
            # claims content it does not have; one that is both is ambiguous
            # about which copy is real.
            models.CheckConstraint(
                condition=(
                    models.Q(data__isnull=True, file__isnull=False)
                    | models.Q(data__isnull=False, file__isnull=True)
                ),
                name="annotations_payload_exactly_one_body",
            ),
        ]
        indexes = [
            models.Index(fields=["format"]),
            models.Index(fields=["file"]),
        ]

    def __str__(self):
        return f"Payload {self.pk} {self.format}"
