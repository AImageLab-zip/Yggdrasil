"""What an annotation is anchored to, and how that thing is identified.

Cornerstone's ``imageId``, ``volumeId`` and ``segmentationId`` are session
identifiers: they encode a URL, they change when a route changes, and they are
meaningless in the next browser tab. None of them may ever reach a column. A
:class:`SourceResource` is Yggdrasil's own durable name for the same thing --
"the volume member ``volume_nifti`` of ``FileRegistry`` 412" -- and every
annotation points at one of these instead.

``identity_key`` is that name, as a single unique string. It is a single column
because of F12: MySQL compiles ``UniqueConstraint(condition=...)`` to *nothing*,
no partial index and no error, so a rule like "unique per file when kind=file,
unique per UID when kind=dicom_series" would look enforced in the model and be
absent in the database. One unconditional unique column cannot be silently
dropped. ``annotations.identity`` builds the string; keeping construction in one
pure function is what makes the column trustworthy.
"""

from django.db import models

from annotations.constants import ResourceKind


class SourceResource(models.Model):
    """A durable, viewer-independent handle on annotatable content."""

    kind = models.CharField(max_length=32, choices=ResourceKind.CHOICES)
    identity_key = models.CharField(
        max_length=255,
        unique=True,
        help_text="Canonical identity string. Built by annotations.identity, never by hand.",
    )

    # PROTECT, not CASCADE: deleting the bytes an annotation was drawn on has to
    # be a decision somebody makes explicitly, not a side effect of tidying up a
    # FileRegistry row. The raw-data lock stops this in the app; PROTECT is what
    # stops it in a shell.
    file = models.ForeignKey(
        "common.FileRegistry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="annotation_resources",
        help_text="Set for kind=file and for bundle members; NULL for DICOM and derived kinds.",
    )
    file_key = models.CharField(
        max_length=60,
        blank=True,
        help_text=(
            "Member key inside a multi-file bundle (FileRegistry.metadata['files']), "
            "e.g. 'volume_nifti'. Blank means the row's own file_path."
        ),
    )

    # DICOM identity, populated from Phase 8 onward. Kept here rather than in a
    # subclass so a single unique identity_key covers every kind.
    sop_instance_uid = models.CharField(max_length=64, blank=True)
    series_instance_uid = models.CharField(max_length=64, blank=True)
    frame_of_reference_uid = models.CharField(
        max_length=64,
        blank=True,
        help_text="Coordinates in patient_lps_mm are only comparable within one of these.",
    )

    content_hash = models.CharField(
        max_length=64,
        blank=True,
        help_text=(
            "SHA-256 of the bytes as of registration. A later mismatch means the "
            "resource was rewritten underneath its annotations -- which is what "
            "the raw-data lock exists to prevent, and what crosscheck reports."
        ),
    )
    descriptor = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Shape, spacing, frame count and other facts needed to interpret "
            "coordinates. Descriptive only: never a substitute for reading the file."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "annotations_source_resource"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["kind"]),
            models.Index(fields=["file", "file_key"]),
            models.Index(fields=["series_instance_uid"]),
            models.Index(fields=["frame_of_reference_uid"]),
        ]

    def __str__(self):
        return f"{self.kind}:{self.identity_key}"
