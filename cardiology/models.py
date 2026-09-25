import logging

from django.contrib.auth.models import User
from django.db import models

from common.models import Modality, Project
from common.base_models import (
    ActivePatientManager,
    ExportBase,
    FolderAccessBase,
    FolderBase,
    TagBase,
    VoiceCaptionBase,
)


logger = logging.getLogger(__name__)


class CardiologyProject(Project):
    """Project proxy bound to the cardiology domain (admin section + forced domain)."""

    class Meta:
        proxy = True
        verbose_name = 'Cardiology project'
        verbose_name_plural = 'Cardiology projects'


class Folder(FolderBase):
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cardiology_folders_created',
    )
    # Mandatory project scope (see maxillo.Folder).
    project = models.ForeignKey(
        'common.Project', on_delete=models.CASCADE,
        related_name='cardiology_folders',
    )

    class Meta:
        db_table = 'cardiology_folder'
        unique_together = ('project', 'name', 'parent')
        ordering = ['name']
        indexes = [
            models.Index(fields=['project']),
            models.Index(fields=['parent']),
            models.Index(fields=['name']),
        ]


class FolderAccess(FolderAccessBase):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='cardiology_folder_access')

    class Meta:
        db_table = 'cardiology_folder_access'
        unique_together = ('user', 'folder')
        indexes = [
            models.Index(fields=['folder']),
            models.Index(fields=['user']),
            models.Index(fields=['role']),
            models.Index(fields=['folder', 'role']),
            models.Index(fields=['user', 'role']),
        ]


class Tag(TagBase):
    class Meta:
        db_table = 'cardiology_tag'
        ordering = ['name']
        indexes = [
            models.Index(fields=['name']),
        ]


class Patient(models.Model):
    VISIBILITY_CHOICES = [
        ('public', 'Public'),
        ('private', 'Private'),
        ('debug', 'Debug'),
    ]

    patient_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, blank=True)
    modalities = models.ManyToManyField(
        Modality,
        blank=True,
        related_name='cardiology_patients',
        help_text='Modalities available for this patient',
    )
    folder = models.ForeignKey('Folder', on_delete=models.SET_NULL, null=True, blank=True, related_name='patients')
    # Mandatory project scope (see maxillo.Patient).
    project = models.ForeignKey(
        'common.Project', on_delete=models.CASCADE,
        related_name='cardiology_patients',
    )
    tags = models.ManyToManyField('Tag', blank=True, related_name='patients')

    visibility = models.CharField(max_length=10, choices=VISIBILITY_CHOICES, default='private')
    deleted = models.BooleanField(default=False, db_index=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='cardiology_patients_uploaded',
    )

    objects = ActivePatientManager()
    all_objects = models.Manager()

    class Meta:
        db_table = 'cardiology_patient'
        ordering = ['-uploaded_at']
        indexes = [
            models.Index(fields=['visibility']),
            models.Index(fields=['uploaded_at']),
            models.Index(fields=['folder']),
            models.Index(fields=['project']),
            models.Index(fields=['name']),
            models.Index(fields=['visibility', 'uploaded_at']),
        ]

    def __str__(self):
        return f"Patient {self.patient_id} - {self.name}"

    @property
    def files(self):
        from common.models import FileRegistry
        return FileRegistry.objects.filter(domain='cardiology', cardiology_patient=self)

    @property
    def processing_jobs(self):
        from common.models import ProcessingJob
        return ProcessingJob.objects.filter(domain='cardiology', cardiology_patient=self)

    def tag_names(self):
        return list(self.tags.values_list('name', flat=True))

    def save(self, *args, **kwargs):
        creating = self._state.adding

        super().save(*args, **kwargs)

        if creating and (self.name is None or self.name.strip() == ''):
            self.name = f"Patient {self.patient_id}"
            super().save(update_fields=['name'])

    def has_ecg(self):
        try:
            return self.files.filter(file_type='ecg_raw').exists()
        except Exception as exc:
            logger.error('Error checking ECG file for cardiology patient %s: %s', self.patient_id, exc, exc_info=True)
            return False

    # Newest-wins, not exactly-one: a re-uploaded recording is ordinary, and
    # `.get()` would raise MultipleObjectsReturned. See laparoscopy's _latest_file.
    def get_ecg_raw_file(self):
        return self.files.filter(file_type='ecg_raw').order_by('-created_at', '-id').first()


class VoiceCaption(VoiceCaptionBase):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='cardiology_voice_captions')
    modality = models.CharField(max_length=255, default='', blank=True)
    text_caption = models.TextField(blank=True, null=True)
    original_text_caption = models.TextField(blank=True, null=True)
    is_edited = models.BooleanField(default=False)
    edit_history = models.JSONField(default=list, blank=True)
    processing_status = models.CharField(max_length=20, choices=VoiceCaptionBase.PROCESSING_STATUS_CHOICES, default='pending')

    class Meta:
        db_table = 'cardiology_voicecaption'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['patient', 'processing_status']),
            models.Index(fields=['processing_status']),
            models.Index(fields=['user']),
        ]



class Export(ExportBase):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='cardiology_exports')
    query_params = models.JSONField(default=dict, help_text='Stores folder_ids, artifacts, and filters')
    query_summary = models.CharField(max_length=500, blank=True, help_text='Human-readable query summary')
    file_path = models.CharField(max_length=1000, blank=True, help_text='Path to generated ZIP file')
    file_size = models.BigIntegerField(default=0, help_text='Size of export file in bytes')
    patient_count = models.IntegerField(default=0, help_text='Number of patients in export')
    started_at = models.DateTimeField(null=True, blank=True, help_text='When processing started')
    completed_at = models.DateTimeField(null=True, blank=True, help_text='When processing completed')
    error_message = models.TextField(blank=True, help_text='Error message if export failed')
    share_mode = models.CharField(
        max_length=20,
        choices=ExportBase.SHARE_MODE_CHOICES,
        default='private',
        help_text='Controls who can access the share link',
    )
    share_token = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        help_text='Random token used for share link access',
    )
    shared_at = models.DateTimeField(null=True, blank=True, help_text='When sharing was last enabled')
    progress_message = models.CharField(max_length=255, blank=True, help_text='Current phase or progress text')
    progress_percent = models.IntegerField(null=True, blank=True, help_text='Progress 0-100')

    class Meta:
        db_table = 'cardiology_export'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['status', 'created_at']),
        ]

    def __str__(self):
        return f"Export {self.id} - {self.get_status_display()}"
