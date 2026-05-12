import secrets

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

import logging

from common.models import Modality


logger = logging.getLogger(__name__)


def laparoscopy_scan_upload_path(instance, filename):
    return f"laparoscopy/patient_{instance.patient_id}/raw/{filename}"


def laparoscopy_normalized_scan_path(instance, filename):
    return f"laparoscopy/patient_{instance.patient_id}/normalized/{filename}"


def laparoscopy_cbct_upload_path(instance, filename):
    return f"laparoscopy/patient_{instance.patient_id}/cbct/{filename}"


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
        related_name='laparoscopy_datasets_created',
    )

    class Meta:
        db_table = 'laparoscopy_dataset'
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
        related_name='laparoscopy_folders_created',
    )

    class Meta:
        db_table = 'laparoscopy_folder'
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


class Tag(models.Model):
    name = models.CharField(max_length=50, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'laparoscopy_tag'
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

    PROCESSING_STATUS_CHOICES = [
        ('not_uploaded', 'Not Uploaded'),
        ('processing', 'Processing'),
        ('processed', 'Processed'),
        ('failed', 'Processing Failed'),
    ]

    patient_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, blank=True)
    dataset = models.ForeignKey(Dataset, on_delete=models.SET_NULL, null=True, blank=True, related_name='patients')
    modalities = models.ManyToManyField(
        Modality,
        blank=True,
        related_name='laparoscopy_patients',
        help_text='Modalities available for this patient',
    )
    folder = models.ForeignKey('Folder', on_delete=models.SET_NULL, null=True, blank=True, related_name='patients')
    tags = models.ManyToManyField('Tag', blank=True, related_name='patients')

    upper_scan_raw = models.FileField(upload_to=laparoscopy_scan_upload_path, blank=True, null=True)
    lower_scan_raw = models.FileField(upload_to=laparoscopy_scan_upload_path, blank=True, null=True)
    upper_scan_norm = models.FileField(upload_to=laparoscopy_normalized_scan_path, blank=True, null=True)
    lower_scan_norm = models.FileField(upload_to=laparoscopy_normalized_scan_path, blank=True, null=True)
    cbct = models.FileField(upload_to=laparoscopy_cbct_upload_path, blank=True, null=True)

    ios_processing_status = models.CharField(
        max_length=20,
        choices=PROCESSING_STATUS_CHOICES,
        default='not_uploaded',
        help_text='Processing status for intra-oral scans (upper and lower)',
    )
    cbct_processing_status = models.CharField(
        max_length=20,
        choices=PROCESSING_STATUS_CHOICES,
        default='not_uploaded',
        help_text='Processing status for CBCT scan',
    )

    visibility = models.CharField(max_length=10, choices=VISIBILITY_CHOICES, default='private')
    deleted = models.BooleanField(default=False, db_index=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='laparoscopy_patients_uploaded',
    )

    objects = ActivePatientManager()
    all_objects = models.Manager()

    class Meta:
        db_table = 'laparoscopy_patient'
        ordering = ['-uploaded_at']
        indexes = [
            models.Index(fields=['visibility']),
            models.Index(fields=['uploaded_at']),
            models.Index(fields=['folder']),
            models.Index(fields=['name']),
            models.Index(fields=['visibility', 'uploaded_at']),
            models.Index(fields=['folder', 'visibility']),
        ]

    def __str__(self):
        return f"Patient {self.patient_id} - {self.name}"

    @property
    def project(self):
        from common.models import Project
        return Project.objects.filter(slug='laparoscopy').first()

    @property
    def project_id(self):
        project = self.project
        return project.id if project else None

    @property
    def files(self):
        from common.models import FileRegistry
        return FileRegistry.objects.filter(domain='laparoscopy', laparoscopy_patient=self)

    @property
    def processing_jobs(self):
        from common.models import ProcessingJob
        return ProcessingJob.objects.filter(domain='laparoscopy', laparoscopy_patient=self)

    def tag_names(self):
        return list(self.tags.values_list('name', flat=True))

    def save(self, *args, **kwargs):
        creating = self._state.adding
        if self.upper_scan_raw and self.lower_scan_raw and self.ios_processing_status == 'not_uploaded':
            self.ios_processing_status = 'processing'
        if self.cbct and self.cbct_processing_status == 'not_uploaded':
            self.cbct_processing_status = 'processing'

        super().save(*args, **kwargs)

        if creating and (self.name is None or self.name.strip() == ''):
            self.name = f"Patient {self.patient_id}"
            super().save(update_fields=['name'])

    def has_ios_scans(self):
        if self.upper_scan_raw and self.lower_scan_raw:
            return True
        try:
            upper_raw = self.files.filter(file_type='ios_raw_upper').exists()
            lower_raw = self.files.filter(file_type='ios_raw_lower').exists()
            upper_processed = self.files.filter(file_type='ios_processed_upper').exists()
            lower_processed = self.files.filter(file_type='ios_processed_lower').exists()
            return (upper_raw or upper_processed) and (lower_raw or lower_processed)
        except Exception as exc:
            logger.error('Error checking IOS files for laparoscopy patient %s: %s', self.patient_id, exc, exc_info=True)
            return False

    def has_cbct_scan(self):
        if self.cbct:
            return True
        try:
            has_raw = self.files.filter(file_type='cbct_raw').exists()
            has_processed = self.files.filter(file_type='cbct_processed').exists()
            return has_raw or has_processed
        except Exception as exc:
            logger.error('Error checking CBCT files for laparoscopy patient %s: %s', self.patient_id, exc, exc_info=True)
            return False

    def has_video(self):
        try:
            return self.files.filter(file_type='video_raw').exists()
        except Exception as exc:
            logger.error('Error checking video files for laparoscopy patient %s: %s', self.patient_id, exc, exc_info=True)
            return False

    def is_ios_processed(self):
        return self.ios_processing_status == 'processed'

    def is_cbct_processed(self):
        return self.cbct_processing_status == 'processed'

    def has_rgb_images(self):
        try:
            return self.files.filter(file_type='rgb_image').exists()
        except Exception as exc:
            logger.error('Error checking RGB files for laparoscopy patient %s: %s', self.patient_id, exc, exc_info=True)
            return False

    def get_rgb_images(self):
        return self.files.filter(file_type='rgb_image').order_by('-created_at')

    def get_raw_files(self):
        return self.files.filter(file_type__in=['video_raw', 'audio_raw'])

    def get_processed_files(self):
        return self.files.filter(file_type__in=['video_processed', 'audio_processed'])

    def get_cbct_raw_file(self):
        from common.models import FileRegistry
        try:
            return self.files.get(file_type='cbct_raw')
        except FileRegistry.DoesNotExist:
            return None

    def get_cbct_processed_file(self):
        from common.models import FileRegistry
        try:
            return self.files.get(file_type='cbct_processed')
        except FileRegistry.DoesNotExist:
            return None

    def get_ios_raw_files(self):
        from common.models import FileRegistry
        upper = None
        lower = None
        try:
            upper = self.files.get(file_type='ios_raw_upper')
        except FileRegistry.DoesNotExist:
            pass
        try:
            lower = self.files.get(file_type='ios_raw_lower')
        except FileRegistry.DoesNotExist:
            pass
        return {'upper': upper, 'lower': lower}

    def get_ios_processed_files(self):
        from common.models import FileRegistry
        upper = None
        lower = None
        try:
            upper = self.files.get(file_type='ios_processed_upper')
        except FileRegistry.DoesNotExist:
            pass
        try:
            lower = self.files.get(file_type='ios_processed_lower')
        except FileRegistry.DoesNotExist:
            pass
        return {'upper': upper, 'lower': lower}


class Classification(models.Model):
    CLASSIFIER_CHOICES = [
        ('manual', 'Manual'),
        ('pipeline', 'Pipeline'),
    ]

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='classifications', null=True, blank=True)
    classifier = models.CharField(max_length=10, choices=CLASSIFIER_CHOICES, default='manual')
    notes = models.TextField(blank=True)
    annotator = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='laparoscopy_classifications_authored',
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'laparoscopy_classification'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['patient', 'classifier']),
            models.Index(fields=['classifier']),
        ]

    def __str__(self):
        return f"Classification {self.id} - {self.get_classifier_display()}"


class RegionType(models.Model):
    project = models.ForeignKey(
        'common.Project', on_delete=models.CASCADE, related_name="laparoscopy_region_types"
    )
    name = models.CharField(max_length=100)
    color = models.CharField(max_length=7, default="#3498db")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'laparoscopy_regiontype'
        ordering = ["order", "name"]
        unique_together = ("project", "name")

    def __str__(self):
        return f"{self.project} / {self.name}"


class QuadrantType(models.Model):
    project = models.ForeignKey(
        'common.Project', on_delete=models.CASCADE, related_name="laparoscopy_quadrant_types"
    )
    name = models.CharField(max_length=100)
    color = models.CharField(max_length=7, default="#e74c3c")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'laparoscopy_quadranttype'
        ordering = ["order", "name"]
        unique_together = ("project", "name")

    def __str__(self):
        return f"{self.project} / {self.name}"


class RegionTypeUserColor(models.Model):
    region_type = models.ForeignKey(
        RegionType, on_delete=models.CASCADE, related_name="user_colors"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="laparoscopy_region_colors"
    )
    color = models.CharField(max_length=7)

    class Meta:
        db_table = 'laparoscopy_regiontypeusercolor'
        unique_together = ("region_type", "user")


class QuadrantTypeUserColor(models.Model):
    quadrant_type = models.ForeignKey(
        QuadrantType, on_delete=models.CASCADE, related_name="user_colors"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="laparoscopy_quadrant_colors"
    )
    color = models.CharField(max_length=7)

    class Meta:
        db_table = 'laparoscopy_quadranttypeusercolor'
        unique_together = ("quadrant_type", "user")


class QuadrantClassificationMarker(models.Model):
    patient = models.ForeignKey(
        'laparoscopy.Patient',
        on_delete=models.CASCADE,
        related_name="quadrant_markers",
    )
    quadrant_type = models.ForeignKey(
        QuadrantType, on_delete=models.CASCADE, related_name="markers"
    )
    time_ms = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        User, null=True, on_delete=models.SET_NULL,
        related_name="created_laparoscopy_quadrant_markers"
    )
    updated_by = models.ForeignKey(
        User, null=True, on_delete=models.SET_NULL,
        related_name="updated_laparoscopy_quadrant_markers"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'laparoscopy_quadrantclassificationmarker'
        ordering = ["time_ms", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["patient", "time_ms"],
                name="laparo_unique_patient_quadrant_marker_time",
            )
        ]
        indexes = [
            models.Index(fields=["patient", "time_ms"]),
            models.Index(fields=["patient", "quadrant_type"]),
        ]

    def __str__(self):
        return f"Marker {self.id} patient {self.patient_id} @ {self.time_ms}ms"


class RegionAnnotation(models.Model):
    TOOL_CHOICES = [
        ("brush", "Brush"),
        ("eraser", "Eraser"),
        ("polygon", "Polygon"),
    ]

    patient = models.ForeignKey(
        'laparoscopy.Patient',
        on_delete=models.CASCADE,
        related_name="region_annotations",
    )
    region_type = models.ForeignKey(
        RegionType, on_delete=models.CASCADE, related_name="annotations"
    )
    tool = models.CharField(max_length=20, choices=TOOL_CHOICES)
    frame_time = models.FloatField(default=0.0)
    points = models.JSONField(default=list)
    prompt_points = models.JSONField(default=list, blank=True)
    stroke_width = models.FloatField(default=1.0)
    created_by = models.ForeignKey(
        User, null=True, on_delete=models.SET_NULL,
        related_name="created_laparoscopy_annotations"
    )
    updated_by = models.ForeignKey(
        User, null=True, on_delete=models.SET_NULL,
        related_name="updated_laparoscopy_annotations"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'laparoscopy_regionannotation'
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["patient", "frame_time"]),
            models.Index(fields=["patient", "region_type"]),
        ]

    def __str__(self):
        return f"Annotation {self.id} ({self.tool}) on patient {self.patient_id}"


class VoiceCaption(models.Model):
    PROCESSING_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='voice_captions', null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='laparoscopy_voice_captions')
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
        db_table = 'laparoscopy_voicecaption'
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
        return FileRegistry.objects.filter(domain='laparoscopy', laparoscopy_voice_caption=self)

    @property
    def processing_jobs(self):
        from common.models import ProcessingJob
        return ProcessingJob.objects.filter(domain='laparoscopy', laparoscopy_voice_caption=self)

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

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='laparoscopy_exports')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    query_params = models.JSONField(default=dict, help_text='Stores folder_ids, modality_slugs, and filters')
    query_summary = models.CharField(max_length=500, blank=True, help_text='Human-readable query summary')
    file_path = models.CharField(max_length=1000, blank=True, help_text='Path to generated ZIP file')
    file_size = models.BigIntegerField(default=0, help_text='Size of export file in bytes')
    patient_count = models.IntegerField(default=0, help_text='Number of patients in export')
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True, help_text='When processing started')
    completed_at = models.DateTimeField(null=True, blank=True, help_text='When processing completed')
    error_message = models.TextField(blank=True, help_text='Error message if export failed')
    share_mode = models.CharField(
        max_length=20,
        choices=SHARE_MODE_CHOICES,
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
        db_table = 'laparoscopy_export'
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
