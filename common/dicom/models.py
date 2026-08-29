"""The DICOM catalog: what was stored, and where each instance lives.

One ``FileRegistry`` row per *series*, whose ``file_path`` is an object-storage prefix
rather than an object. That is not new: ``maxillo.file_utils.save_generic_modality_folder``
has stored folder uploads that way since before this phase, and every consumer that
already handles a prefix row handles a series for free. What is new is that the members
are addressable individually, because a viewer asks for one frame at a time.

These models live in their own module rather than in the 950-line ``common/models.py``,
with an explicit ``app_label`` so Django still files them under ``common``.
``common.models`` re-exports them, so ``from common.models import DicomSeries`` reads the
same as every other model in the project and no caller needs to know where the file is.
"""

from django.db import models
from django.utils import timezone


class SealedSeriesError(RuntimeError):
    """An instance of a sealed series was about to be rewritten.

    The annotation lock (``common/annotation_lock.py``) guards ``FileRegistry`` *rows*:
    it refuses to add or remove a raw file once annotation work exists. A DICOM series
    is one row holding hundreds of objects, so every one of those objects is invisible
    to it -- rewriting instance 137 in place would re-base every coordinate drawn on
    the volume with nothing in the record to say so. ``sealed_at`` closes that gap at
    the only place that can see it, which is the instance's own ``save()``.
    """


class DicomSeries(models.Model):
    """One DICOM series, stored natively, as one ``FileRegistry`` prefix row."""

    file = models.OneToOneField(
        "common.FileRegistry",
        on_delete=models.CASCADE,
        related_name="dicom_series",
        help_text="The prefix row whose file_path is this series' object-storage prefix.",
    )

    # Pseudonymous throughout: these are what common.dicom.deidentify derived, never
    # what arrived. The originals are not stored anywhere -- see that module for why
    # there is no mapping table.
    series_instance_uid = models.CharField(max_length=64, unique=True)
    study_instance_uid = models.CharField(max_length=64, db_index=True)
    frame_of_reference_uid = models.CharField(
        max_length=64,
        blank=True,
        help_text=(
            "Coordinates are only comparable within one of these. Mirrors "
            "annotations.SourceResource.frame_of_reference_uid, which is what an "
            "annotation on this series actually anchors to."
        ),
    )

    # The DICOM Modality tag ('CT', 'MR'), which is *not* a Yggdrasil Modality slug.
    # Named apart so the two are never confused in a filter.
    dicom_modality = models.CharField(max_length=16, blank=True)
    sop_class_uid = models.CharField(max_length=64, blank=True)
    transfer_syntax_uid = models.CharField(max_length=64, blank=True)

    instance_count = models.PositiveIntegerField(default=0)
    rows = models.PositiveIntegerField(default=0)
    columns = models.PositiveIntegerField(default=0)

    deid_profile = models.CharField(
        max_length=60,
        blank=True,
        help_text="Which de-identification produced the stored bytes.",
    )
    deid_confidence = models.CharField(
        max_length=32,
        blank=True,
        help_text=(
            "What the de-identification actually established. 'header_only' means the "
            "pixels were never examined; nothing may present that to a user as "
            "'de-identified'."
        ),
    )

    sealed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Set once annotation work exists on this series. After it, no instance "
            "may be rewritten -- the lock guards FileRegistry rows and cannot see "
            "inside one."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "common"
        verbose_name_plural = "DICOM series"
        indexes = [
            models.Index(fields=["study_instance_uid", "series_instance_uid"]),
        ]

    def __str__(self):
        return f"DicomSeries {self.series_instance_uid} ({self.instance_count} instances)"

    @property
    def is_sealed(self):
        return self.sealed_at is not None

    def seal(self):
        """Freeze this series' instances. Idempotent, and never reversed.

        Monotonic for the same reason the annotation lock is (decision #18): the bytes
        were already interpreted, and a seal that can be quietly lifted is one nobody
        can explain afterwards.
        """
        if self.sealed_at is None:
            self.sealed_at = timezone.now()
            self.save(update_fields=["sealed_at"])
        return self.sealed_at


class DicomInstance(models.Model):
    """One SOP instance: the smallest thing a viewer or an export addresses."""

    series = models.ForeignKey(
        DicomSeries, on_delete=models.CASCADE, related_name="instances"
    )
    sop_instance_uid = models.CharField(max_length=64, unique=True)
    instance_number = models.IntegerField(default=0)

    object_key = models.CharField(
        max_length=500,
        unique=True,
        help_text="Object-storage key of this instance, under the series prefix.",
    )
    file_size = models.BigIntegerField(default=0)
    content_hash = models.CharField(max_length=64, blank=True)
    frame_count = models.PositiveIntegerField(default=1)

    # Geometry, kept out of the header so slice ordering and the metadata response do
    # not have to re-read several hundred objects to answer one request. Descriptive,
    # never a substitute for the file -- the same rule SourceResource.descriptor states.
    image_position_patient = models.JSONField(default=list, blank=True)
    image_orientation_patient = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "common"
        ordering = ["instance_number", "sop_instance_uid"]
        indexes = [
            models.Index(fields=["series", "instance_number"]),
        ]

    def __str__(self):
        return f"DicomInstance {self.sop_instance_uid}"

    def save(self, *args, **kwargs):
        """Refuse to write an instance whose series is sealed.

        Insert and update alike: a *new* instance appearing in a sealed series changes
        the volume just as much as an edited one, and an annotation drawn on 300 slices
        does not describe 301.
        """
        if self.series_id:
            sealed = (
                DicomSeries.objects.filter(pk=self.series_id)
                .values_list("sealed_at", flat=True)
                .first()
            )
            if sealed is not None:
                raise SealedSeriesError(
                    f"DICOM series {self.series_id} is sealed (annotation work exists "
                    f"on it), so instance {self.sop_instance_uid or '(new)'} may not "
                    "be written."
                )
        return super().save(*args, **kwargs)
