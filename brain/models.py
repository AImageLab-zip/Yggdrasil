import secrets

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

import logging

from common.models import Modality, Project
from common.base_models import (
    ActivePatientManager,
    DatasetBase,
    FolderAccessBase,
    FolderBase,
    TagBase,
    ExportBase,
    VoiceCaptionBase,
)


logger = logging.getLogger(__name__)


class Dataset(DatasetBase):
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='brain_datasets_created',
    )

    class Meta:
        db_table = 'brain_dataset'
        ordering = ['name']


class BrainProject(Project):
    """Project proxy bound to the brain domain (admin section + forced domain)."""

    class Meta:
        proxy = True
        verbose_name = 'Brain project'
        verbose_name_plural = 'Brain projects'


class Folder(FolderBase):
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='brain_folders_created',
    )
    # Mandatory project scope (see maxillo.Folder).
    project = models.ForeignKey(
        'common.Project', on_delete=models.CASCADE,
        related_name='brain_folders',
    )

    class Meta:
        db_table = 'brain_folder'
        unique_together = ('project', 'name', 'parent')
        ordering = ['name']
        indexes = [
            models.Index(fields=['project']),
            models.Index(fields=['parent']),
            models.Index(fields=['name']),
        ]


class FolderAccess(FolderAccessBase):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='brain_folder_access')

    class Meta:
        db_table = 'brain_folder_access'
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
        db_table = 'brain_tag'
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
    dataset = models.ForeignKey(Dataset, on_delete=models.SET_NULL, null=True, blank=True, related_name='patients')
    modalities = models.ManyToManyField(
        Modality,
        blank=True,
        related_name='brain_patients',
        help_text='Modalities available for this patient',
    )
    # Single folder per patient (was a M2M; collapsed by the folder->project
    # migration, first folder wins).
    folder = models.ForeignKey('Folder', on_delete=models.SET_NULL, null=True, blank=True, related_name='patients')
    # Mandatory project scope (see maxillo.Patient).
    project = models.ForeignKey(
        'common.Project', on_delete=models.CASCADE,
        related_name='brain_patients',
    )
    tags = models.ManyToManyField('Tag', blank=True, related_name='patients')

    visibility = models.CharField(max_length=10, choices=VISIBILITY_CHOICES, default='private')
    deleted = models.BooleanField(default=False, db_index=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='brain_patients_uploaded',
    )

    objects = ActivePatientManager()
    all_objects = models.Manager()

    class Meta:
        db_table = 'brain_patient'
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

        return FileRegistry.objects.filter(domain='brain', brain_patient=self)

    @property
    def jobs(self):
        from common.models import Job

        return Job.objects.filter(domain='brain', brain_patient=self)

    @property
    def processing_jobs(self):
        from common.models import ProcessingJob

        return ProcessingJob.objects.filter(domain='brain', brain_patient=self)

    def tag_names(self):
        return list(self.tags.values_list('name', flat=True))

    def save(self, *args, **kwargs):
        creating = self._state.adding

        super().save(*args, **kwargs)

        if creating and (self.name is None or self.name.strip() == ''):
            self.name = f"Patient {self.patient_id}"
            super().save(update_fields=['name'])

    def has_ios_scans(self):
        try:
            upper_raw = self.files.filter(file_type='ios_raw_upper').exists()
            lower_raw = self.files.filter(file_type='ios_raw_lower').exists()
            upper_processed = self.files.filter(file_type='ios_processed_upper').exists()
            lower_processed = self.files.filter(file_type='ios_processed_lower').exists()
            return (upper_raw or upper_processed) and (lower_raw or lower_processed)
        except Exception as exc:
            logger.error('Error checking IOS files for brain patient %s: %s', self.patient_id, exc, exc_info=True)
            return False

    def has_cbct_scan(self):
        try:
            has_raw = self.files.filter(file_type='cbct_raw').exists()
            has_processed = self.files.filter(file_type='cbct_processed').exists()
            return has_raw or has_processed
        except Exception as exc:
            logger.error('Error checking CBCT files for brain patient %s: %s', self.patient_id, exc, exc_info=True)
            return False

    def _processing_status(self, modality_slug):
        from common.job_routing import is_runner_enabled_for_modality
        from common.modality_config import modality_is_blocking

        job = self.jobs.filter(modality_slug=modality_slug).order_by('-created_at').first()
        if not is_runner_enabled_for_modality(modality_slug):
            if job and job.status == 'completed':
                return 'processed'
            base = str(modality_slug or '').replace('-', '_')
            file_types = [f'{base}_raw', f'{base}_processed']
            if modality_slug == 'cbct':
                file_types = ['cbct_raw', 'cbct_processed']
            elif modality_slug == 'ios':
                return 'processed' if self.has_ios_scans() else 'not_uploaded'
            if self.files.filter(file_type__in=file_types).exists():
                return 'processed'
            if self.files.filter(modality__slug=modality_slug).exists():
                return 'processed'
            return 'not_uploaded'

        if not job:
            return 'not_uploaded'
        if job.status in ('pending', 'processing', 'retrying'):
            # Non-blocking modalities don't gate readiness (Phase 4).
            return 'processing' if modality_is_blocking(modality_slug) else 'processed'
        if job.status == 'failed':
            return 'failed'
        if job.status == 'completed':
            return 'processed'
        return 'not_uploaded'

    @property
    def ios_job_status(self):
        return self._processing_status('ios')

    @property
    def cbct_job_status(self):
        return self._processing_status('cbct')

    def is_ios_processed(self):
        return self.ios_job_status == 'processed'

    def is_cbct_processed(self):
        return self.cbct_job_status == 'processed'

    def has_rgb_images(self):
        try:
            return self.files.filter(file_type='rgb_image').exists()
        except Exception as exc:
            logger.error('Error checking RGB files for brain patient %s: %s', self.patient_id, exc, exc_info=True)
            return False

    def get_rgb_images(self):
        return self.files.filter(file_type='rgb_image').order_by('-created_at')


class VoiceCaption(VoiceCaptionBase):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='brain_voice_captions')
    modality = models.CharField(max_length=255, default='', blank=True)
    text_caption = models.TextField(blank=True, null=True)
    original_text_caption = models.TextField(blank=True, null=True)
    is_edited = models.BooleanField(default=False)
    edit_history = models.JSONField(default=list, blank=True)
    processing_status = models.CharField(max_length=20, choices=VoiceCaptionBase.PROCESSING_STATUS_CHOICES, default='pending')

    class Meta:
        db_table = 'brain_voicecaption'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['patient', 'processing_status']),
            models.Index(fields=['processing_status']),
            models.Index(fields=['user']),
        ]


class Export(ExportBase):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='brain_exports')
    query_params = models.JSONField(default=dict)
    query_summary = models.CharField(max_length=500, blank=True)
    file_path = models.CharField(max_length=1000, blank=True)
    file_size = models.BigIntegerField(default=0)
    patient_count = models.IntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    share_mode = models.CharField(max_length=20, choices=ExportBase.SHARE_MODE_CHOICES, default='private')
    share_token = models.CharField(max_length=64, unique=True, null=True, blank=True)
    shared_at = models.DateTimeField(null=True, blank=True)
    progress_message = models.CharField(max_length=255, blank=True)
    progress_percent = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = 'brain_export'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['status', 'created_at']),
        ]

    def __str__(self):
        return f"Export {self.id} - {self.get_status_display()}"


class UserPreference(models.Model):
    """Stores per-user UI preferences for the Brain app."""

    LANGUAGE_CHOICES = [
        ('it', 'Italian'),
        ('en', 'English'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='brain_preference')
    report_language = models.CharField(max_length=5, choices=LANGUAGE_CHOICES, default='it')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'brain_user_preference'

    def __str__(self):
        return f"Preferences for {self.user.username}"

