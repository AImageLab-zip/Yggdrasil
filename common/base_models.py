"""Abstract base models shared by the domain apps (Phase 5.3).

These are **abstract** bases: each domain app subclasses them into a concrete
model that pins the existing ``db_table`` so there is **zero data migration** —
the physical tables (``maxillo_voicecaption``, ``brain_voicecaption``, ...) are
untouched. The base carries only the fields that are byte-identical across all
three copies plus the shared behavior; fields that have drifted between apps
(different ``help_text``/``default``/``related_name``) stay overridden on the
concrete subclasses. Acceptance gate: ``makemigrations --check`` is empty.

Unqualified string relations (e.g. ``ForeignKey('Patient')``) resolve against
the concrete subclass's app, so each domain's model points at its own Patient.
"""

import secrets

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class ActivePatientManager(models.Manager):
    """Default manager that hides soft-deleted patients."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted=False)


class DatasetBase(models.Model):
    """Shared Dataset fields. ``created_by`` stays on subclasses (its
    ``related_name`` drifts per app), as does any app-specific count helper."""

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True

    def __str__(self):
        return self.name


class FolderBase(models.Model):
    """Shared Folder tree fields/behavior. ``created_by`` stays on subclasses."""

    name = models.CharField(max_length=100)
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True, related_name='children'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_demo = models.BooleanField(
        default=False,
        help_text=(
            'Expose this folder (and its patients) in the anonymous public demo '
            'at /demo/<domain>/. Only anonymized or synthetic studies may be flagged.'
        ),
    )

    class Meta:
        abstract = True

    def __str__(self):
        return self.get_full_path()

    def get_full_path(self):
        parts = []
        node = self
        while node:
            parts.append(node.name)
            node = node.parent
        return '/'.join(reversed(parts))


class FolderAccessBase(models.Model):
    """Shared FolderAccess fields. ``user`` stays on subclasses (its
    ``related_name`` drifts per app); ``folder`` resolves to the subclass app."""

    ROLE_CHOICES = [
        ('standard', 'Standard User'),
        ('annotator', 'Annotator'),
        ('project_manager', 'Project Manager'),
    ]

    folder = models.ForeignKey('Folder', on_delete=models.CASCADE, related_name='access_list')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='standard')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def __str__(self):
        return f"{self.user.username} -> {self.folder.name} ({self.role})"


class TagBase(models.Model):
    """Shared Tag fields/behavior."""

    name = models.CharField(max_length=50, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True

    def __str__(self):
        return self.name


class ClassificationBase(models.Model):
    """Shared Classification fields (maxillo + laparoscopy). The classifier
    choices, patient link and timestamp are common; each app's domain-specific
    classification fields, ``classifier`` default and ``annotator`` related_name
    stay on the subclass."""

    CLASSIFIER_CHOICES = [
        ('manual', 'Manual'),
        ('pipeline', 'Pipeline'),
    ]

    patient = models.ForeignKey(
        'Patient', on_delete=models.CASCADE, related_name='classifications',
        null=True, blank=True,
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class ExportBase(models.Model):
    """Shared dataset-export state machine + share-link lifecycle.

    Only the fields identical across all three apps are lifted (status,
    created_at, expires_at); the rest carry per-app help_text drift and stay
    on subclasses. The mark_*/ensure_share_token methods (previously copy-pasted
    into each app) live here so the export lifecycle has a single definition.
    ``__str__`` stays on subclasses (maxillo's is richer). Referencing fields
    like ``self.share_token``/``self.progress_message`` resolves against the
    concrete subclass at runtime.
    """

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

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        null=True, blank=True, help_text='Share link expiry; null = never expires'
    )

    class Meta:
        abstract = True

    def mark_processing(self):
        """Mark export as processing and set started_at timestamp"""
        self.status = 'processing'
        self.started_at = timezone.now()
        self.save()

    def mark_completed(self, file_path=None, file_size=None):
        """Mark export as completed and set file information"""
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
        """Mark export as failed and set error message"""
        self.status = 'failed'
        self.completed_at = timezone.now()
        self.error_message = error_message
        self.save()

    def ensure_share_token(self, force_new=False):
        """Create and persist a share token when needed."""
        if force_new or not self.share_token:
            self.share_token = secrets.token_urlsafe(32)
            self.save(update_fields=['share_token'])
        return self.share_token


class VoiceCaptionBase(models.Model):
    """Audio/text caption attached to a patient, transcribed by a worker.

    Consolidates the three formerly-duplicated VoiceCaption copies. Behavior
    (all the get_*/edit_transcription/files/... methods) lives here so every
    domain gets the same complete implementation — historically maxillo lacked
    the explicit ``files``/``processing_jobs`` properties and ``__str__`` that
    brain and laparoscopy had.
    """

    PROCESSING_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    # Identical across all three apps -> lifted to the base. 'Patient' resolves
    # to the concrete subclass's own app model.
    patient = models.ForeignKey(
        'Patient', on_delete=models.CASCADE, related_name='voice_captions',
        null=True, blank=True,
    )
    duration = models.FloatField(help_text='Duration of audio recording in seconds')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def __str__(self):
        return f"VoiceCaption {self.id} - {self.patient_id}"

    # --- registry-driven related querysets (domain resolved from app_label) ---

    @property
    def files(self):
        from common.domains import fk_fields_for
        from common.models import FileRegistry

        domain = self._meta.app_label
        _, voice_fk = fk_fields_for(domain)
        return FileRegistry.objects.filter(domain=domain, **{voice_fk: self})

    @property
    def processing_jobs(self):
        from common.domains import fk_fields_for
        from common.models import ProcessingJob

        domain = self._meta.app_label
        _, voice_fk = fk_fields_for(domain)
        return ProcessingJob.objects.filter(domain=domain, **{voice_fk: self})

    # --- display helpers ---

    def get_modality_display(self):
        try:
            from common.models import Modality
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
        return (
            self.processing_status == 'completed'
            and self.text_caption
            and self.text_caption != '[Audio processed but no transcription available]'
        )

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

    # --- transcription editing ---

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

    # --- FileRegistry / job lookups ---

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
