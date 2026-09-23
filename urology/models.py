import logging

from django.contrib.auth.models import User
from django.db import models

from common.base_models import (
    ActivePatientManager,
    ExportBase,
    FolderAccessBase,
    FolderBase,
    TagBase,
    VoiceCaptionBase,
)
from common.models import Modality, Project

logger = logging.getLogger(__name__)


class UrologyProject(Project):
    """Project proxy bound to the urology domain (admin section + forced domain)."""

    class Meta:
        proxy = True
        verbose_name = "Urology project"
        verbose_name_plural = "Urology projects"


class Folder(FolderBase):
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="urology_folders_created",
    )
    project = models.ForeignKey(
        "common.Project",
        on_delete=models.CASCADE,
        related_name="urology_folders",
    )

    class Meta:
        db_table = "urology_folder"
        unique_together = ("project", "name", "parent")
        ordering = ["name"]
        indexes = [
            models.Index(fields=["project"]),
            models.Index(fields=["parent"]),
            models.Index(fields=["name"]),
        ]


class FolderAccess(FolderAccessBase):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="urology_folder_access"
    )

    class Meta:
        db_table = "urology_folder_access"
        unique_together = ("user", "folder")
        indexes = [
            models.Index(fields=["folder"]),
            models.Index(fields=["user"]),
            models.Index(fields=["role"]),
            models.Index(fields=["folder", "role"]),
            models.Index(fields=["user", "role"]),
        ]


class Tag(TagBase):
    class Meta:
        db_table = "urology_tag"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["name"]),
        ]


class Patient(models.Model):
    VISIBILITY_CHOICES = [
        ("public", "Public"),
        ("private", "Private"),
        ("debug", "Debug"),
    ]

    patient_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, blank=True)
    modalities = models.ManyToManyField(
        Modality,
        blank=True,
        related_name="urology_patients",
        help_text="Modalities available for this patient",
    )
    folder = models.ForeignKey(
        "Folder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="patients",
    )
    project = models.ForeignKey(
        "common.Project",
        on_delete=models.CASCADE,
        related_name="urology_patients",
    )
    tags = models.ManyToManyField("Tag", blank=True, related_name="patients")
    visibility = models.CharField(
        max_length=10, choices=VISIBILITY_CHOICES, default="private"
    )
    deleted = models.BooleanField(default=False, db_index=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="urology_patients_uploaded",
    )

    objects = ActivePatientManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "urology_patient"
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(fields=["visibility"]),
            models.Index(fields=["uploaded_at"]),
            models.Index(fields=["folder"]),
            models.Index(fields=["project"]),
            models.Index(fields=["name"]),
            models.Index(fields=["visibility", "uploaded_at"]),
        ]

    def __str__(self):
        return f"Patient {self.patient_id} - {self.name}"

    @property
    def files(self):
        from common.models import FileRegistry

        return FileRegistry.objects.filter(domain="urology", urology_patient=self)

    @property
    def jobs(self):
        from common.models import Job

        return Job.objects.filter(domain="urology", urology_patient=self)

    @property
    def processing_jobs(self):
        from common.models import ProcessingJob

        return ProcessingJob.objects.filter(domain="urology", urology_patient=self)

    def tag_names(self):
        return list(self.tags.values_list("name", flat=True))

    def save(self, *args, **kwargs):
        creating = self._state.adding

        super().save(*args, **kwargs)

        if creating and (self.name is None or self.name.strip() == ""):
            self.name = f"Patient {self.patient_id}"
            super().save(update_fields=["name"])

    def _processing_status(self, modality_slug):
        from common.job_routing import is_runner_enabled_for_modality
        from common.modality_config import modality_is_blocking

        job = (
            self.jobs.filter(modality_slug=modality_slug)
            .order_by("-created_at")
            .first()
        )
        if not is_runner_enabled_for_modality(modality_slug):
            if job and job.status == "completed":
                return "processed"
            base = str(modality_slug or "").replace("-", "_")
            file_types = [f"{base}_raw", f"{base}_processed"]
            if self.files.filter(file_type__in=file_types).exists():
                return "processed"
            if self.files.filter(modality__slug=modality_slug).exists():
                return "processed"
            return "not_uploaded"

        if not job:
            return "not_uploaded"
        if job.status in ("pending", "processing", "retrying"):
            return "processing" if modality_is_blocking(modality_slug) else "processed"
        if job.status == "failed":
            return "failed"
        if job.status == "completed":
            return "processed"
        return "not_uploaded"

    @property
    def urology_mri_job_status(self):
        return self._processing_status("urology-mri")

    @property
    def urology_wsi_job_status(self):
        return self._processing_status("urology-wsi")

    @property
    def urology_confocal_job_status(self):
        return self._processing_status("urology-confocal")


class VoiceCaption(VoiceCaptionBase):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="urology_voice_captions"
    )
    modality = models.CharField(max_length=255, default="", blank=True)
    text_caption = models.TextField(blank=True, null=True)
    original_text_caption = models.TextField(blank=True, null=True)
    is_edited = models.BooleanField(default=False)
    edit_history = models.JSONField(default=list, blank=True)
    processing_status = models.CharField(
        max_length=20,
        choices=VoiceCaptionBase.PROCESSING_STATUS_CHOICES,
        default="pending",
    )

    class Meta:
        db_table = "urology_voicecaption"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["patient", "processing_status"]),
            models.Index(fields=["processing_status"]),
            models.Index(fields=["user"]),
        ]


class Export(ExportBase):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="urology_exports"
    )
    query_params = models.JSONField(default=dict)
    query_summary = models.CharField(max_length=500, blank=True)
    file_path = models.CharField(max_length=1000, blank=True)
    file_size = models.BigIntegerField(default=0)
    patient_count = models.IntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    share_mode = models.CharField(
        max_length=20, choices=ExportBase.SHARE_MODE_CHOICES, default="private"
    )
    share_token = models.CharField(max_length=64, unique=True, null=True, blank=True)
    shared_at = models.DateTimeField(null=True, blank=True)
    progress_message = models.CharField(max_length=255, blank=True)
    progress_percent = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "urology_export"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"Export {self.id} - {self.get_status_display()}"
