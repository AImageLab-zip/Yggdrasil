import secrets

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

import logging

from common.models import Modality


logger = logging.getLogger(__name__)


class ActivePatientManager(models.Manager):
    """Default manager that hides soft-deleted patients."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted=False)


class Dataset(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='brain_datasets_created',
    )

    class Meta:
        db_table = 'brain_dataset'
        ordering = ['name']

    def __str__(self):
        return self.name


class Folder(models.Model):
    name = models.CharField(max_length=100)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='brain_folders_created',
    )

    class Meta:
        db_table = 'brain_folder'
        unique_together = ('name', 'parent')
        ordering = ['name']
        indexes = [
            models.Index(fields=['parent']),
            models.Index(fields=['name']),
        ]

    def __str__(self):
        return self.get_full_path()

    def get_full_path(self):
        parts = []
        node = self
        while node:
            parts.append(node.name)
            node = node.parent
        return '/'.join(reversed(parts))


class FolderAccess(models.Model):
    ROLE_CHOICES = [
        ('standard', 'Standard User'),
        ('annotator', 'Annotator'),
        ('project_manager', 'Project Manager'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='brain_folder_access')
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE, related_name='access_list')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='standard')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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

    def __str__(self):
        return f"{self.user.username} -> {self.folder.name} ({self.role})"


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'brain_tag'
        ordering = ['name']
        indexes = [
            models.Index(fields=['name']),
        ]

    def __str__(self):
        return self.name


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
    folders = models.ManyToManyField('Folder', blank=True, related_name='patients')
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

    def has_rgb_images(self):
        try:
            return self.files.filter(file_type='rgb_image').exists()
        except Exception as exc:
            logger.error('Error checking RGB files for brain patient %s: %s', self.patient_id, exc, exc_info=True)
            return False

    def get_rgb_images(self):
        return self.files.filter(file_type='rgb_image').order_by('-created_at')


class VoiceCaption(models.Model):
    PROCESSING_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='voice_captions', null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='brain_voice_captions')
    modality = models.CharField(max_length=255, default='', blank=True)
    duration = models.FloatField(help_text='Duration of audio recording in seconds')
    text_caption = models.TextField(blank=True, null=True)
    original_text_caption = models.TextField(blank=True, null=True)
    is_edited = models.BooleanField(default=False)
    edit_history = models.JSONField(default=list, blank=True)
    processing_status = models.CharField(max_length=20, choices=PROCESSING_STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'brain_voicecaption'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['patient', 'processing_status']),
            models.Index(fields=['processing_status']),
            models.Index(fields=['user']),
        ]

    def __str__(self):
        return f"VoiceCaption {self.id} - {self.patient_id}"

    @property
    def files(self):
        from common.models import FileRegistry

        return FileRegistry.objects.filter(domain='brain', brain_voice_caption=self)

    @property
    def processing_jobs(self):
        from common.models import ProcessingJob

        return ProcessingJob.objects.filter(domain='brain', brain_voice_caption=self)

    def get_modality_display(self):
        try:
            if not self.modality:
                return 'Undefined'
            mod = Modality.objects.filter(slug=self.modality).first()
            if mod:
                return getattr(mod, 'label', '') or getattr(mod, 'name', '') or self.modality.upper()
            return self.modality.upper()
        except Exception:
            return (self.modality or 'Undefined').upper()

    def get_display_duration(self):
        if self.duration == 0:
            return 'Text'
        minutes = int(self.duration // 60)
        seconds = int(self.duration % 60)
        if minutes > 0:
            return f"{minutes}:{seconds:02d}"
        return f"{seconds}s"

    def get_quality_status(self):
        if self.duration == 0:
            return {'color': 'success', 'message': 'Text'}
        if self.duration <= 30:
            return {'color': 'danger', 'message': 'Short'}
        if self.duration <= 45:
            return {'color': 'warning', 'message': 'Good'}
        return {'color': 'success', 'message': 'Perfect'}

    def is_processed(self):
        if self.duration == 0:
            return self.processing_status == 'completed' and self.text_caption
        return self.processing_status == 'completed' and self.text_caption and self.text_caption != '[Audio processed but no transcription available]'

    def get_processing_display_text(self):
        if self.processing_status == 'completed':
            if self.text_caption and self.text_caption != '[Audio processed but no transcription available]':
                return self.text_caption
            return '[Audio processed but no transcription available]'
        if self.processing_status == 'processing':
            return 'Converting speech to text...'
        if self.processing_status == 'failed':
            return 'Processing failed'
        return 'Preprocessing audio...'

    def get_display_text_caption(self):
        if self.is_processed():
            text = self.text_caption
            if self.is_edited:
                text += ' [edited]'
            return text
        return self.get_processing_display_text()

    def save_original_transcription(self):
        if self.text_caption and not self.original_text_caption:
            self.original_text_caption = self.text_caption

    def edit_transcription(self, new_text, user):
        if not self.is_processed():
            raise ValueError('Cannot edit transcription that is not yet processed')
        if not self.original_text_caption:
            self.original_text_caption = self.text_caption
        edit_record = {
            'timestamp': timezone.now().isoformat(),
            'user_id': user.id,
            'username': user.username,
            'previous_text': self.text_caption,
            'new_text': new_text,
        }
        if not self.edit_history:
            self.edit_history = []
        self.edit_history.append(edit_record)
        self.text_caption = new_text
        self.is_edited = True
        self.save()

    def revert_to_original(self, user):
        if not self.original_text_caption:
            raise ValueError('No original transcription to revert to')
        revert_record = {
            'timestamp': timezone.now().isoformat(),
            'user_id': user.id,
            'username': user.username,
            'action': 'reverted_to_original',
            'previous_text': self.text_caption,
            'reverted_text': self.original_text_caption,
        }
        if not self.edit_history:
            self.edit_history = []
        self.edit_history.append(revert_record)
        self.text_caption = self.original_text_caption
        self.is_edited = False
        self.save()

    def get_audio_file(self):
        from common.models import FileRegistry

        try:
            return self.files.get(file_type='audio_raw')
        except FileRegistry.DoesNotExist:
            return None

    def get_processed_text_file(self):
        from common.models import FileRegistry

        try:
            return self.files.get(file_type='audio_processed')
        except FileRegistry.DoesNotExist:
            return None

    def get_pending_jobs(self):
        return self.processing_jobs.filter(status__in=['pending', 'processing', 'retrying'])


class Export(models.Model):
    SHARE_MODE_CHOICES = [
        ('private', 'Private'),
        ('authenticated', 'Any logged-in user'),
        ('public', 'Anyone with link'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='brain_exports')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    query_params = models.JSONField(default=dict)
    query_summary = models.CharField(max_length=500, blank=True)
    file_path = models.CharField(max_length=1000, blank=True)
    file_size = models.BigIntegerField(default=0)
    patient_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    share_mode = models.CharField(max_length=20, choices=SHARE_MODE_CHOICES, default='private')
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

    def mark_processing(self):
        self.status = 'processing'
        self.started_at = timezone.now()
        self.save()

    def mark_completed(self, file_path=None, file_size=None):
        self.status = 'completed'
        self.completed_at = timezone.now()
        self.progress_message = ''
        self.progress_percent = None
        if file_path:
            self.file_path = file_path
        if file_size is not None:
            self.file_size = file_size
        self.save()

    def mark_failed(self, error_message=''):
        self.status = 'failed'
        self.completed_at = timezone.now()
        self.error_message = error_message
        self.save()

    def ensure_share_token(self, force_new=False):
        if force_new or not self.share_token:
            self.share_token = secrets.token_urlsafe(32)
            self.save(update_fields=['share_token'])
        return self.share_token


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

