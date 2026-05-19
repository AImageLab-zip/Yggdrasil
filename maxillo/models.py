import secrets

from django.db import models
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
import os
from django.utils import timezone
from django.utils.text import slugify
from common.models import Modality, ProjectAccess, Job, FileRegistry, Invitation
import logging
logger = logging.getLogger(__name__)


def validate_cbct_folder(files):
    """
    Validator for CBCT folder uploads.
    Validates that the folder contains valid DICOM files.
    
    Args:
        files: List of uploaded files from folder
    """
    if not files:
        raise ValidationError('No files found in uploaded folder')
    
    # Check if any files have DICOM extensions or are DICOMDIR
    valid_files = []
    has_dicomdir = False
    dicom_files = []
    extensionless_files = []
    
    for file in files:
        filename = file.name.lower()
        
        # Check for DICOMDIR (handle both root and nested paths)
        if filename.endswith('dicomdir') or filename == 'dicomdir' or '/dicomdir' in filename:
            has_dicomdir = True
            valid_files.append(file)
        # Check for DICOM files (handle both root and nested paths)
        elif filename.endswith(('.dcm', '.dicom')) or '.dcm' in filename or '.dicom' in filename:
            dicom_files.append(file.name)
            valid_files.append(file)
        # Check for files without extensions (common for DICOM files)
        elif '.' not in os.path.basename(filename):
            extensionless_files.append(file.name)
            valid_files.append(file)
    

    
    if not valid_files:
        # Provide more detailed error message
        file_extensions = set()
        for file in files[:10]:  # Check first 10 files for debugging
            if hasattr(file, 'name'):
                ext = os.path.splitext(file.name.lower())[1]
                file_extensions.add(ext)
        
        error_msg = f'Folder must contain DICOM files (.dcm, .dicom, or no extension) and/or a DICOMDIR file. '
        if file_extensions:
            error_msg += f'Found file types: {", ".join(sorted(file_extensions))}'
        else:
            error_msg += 'No valid files found.'
        
        raise ValidationError(error_msg)
    
    # Validate individual DICOM files (basic check)
    for file in valid_files[:5]:  # Check first 5 files to avoid overwhelming validation
        try:
            # Basic file size check (DICOM files should not be empty)
            if file.size == 0:
                raise ValidationError(f'Invalid DICOM file: {file.name} (empty file)')
        except AttributeError:
            pass  # Skip if size attribute not available
    
    return valid_files


# MaxilloUserProfile has been removed - user permissions are now handled via common.ProjectAccess
# Users get ProjectAccess entries when they accept an invitation or are granted access by an admin


class Dataset(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    
    def __str__(self):
        return self.name
    

    def scan_count(self):
        return self.patients.count()

    def patient_count(self):
        return self.patients.count()

class Folder(models.Model):
    name = models.CharField(max_length=100)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children')
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    
    class Meta:
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

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='maxillo_folder_access')
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE, related_name='access_list')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='standard')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
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
        ordering = ['name']
        indexes = [
            models.Index(fields=['name']),
        ]
    
    def __str__(self):
        return self.name



class ActivePatientManager(models.Manager):
    """Default manager that hides soft-deleted patients."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted=False)


class Patient(models.Model):
    """Patient model - renamed from ScanPair, represents a patient with associated scans and data"""
    VISIBILITY_CHOICES = [
        ('public', 'Public'),
        ('private', 'Private'),
        ('debug', 'Debug'),
    ]
    
    patient_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, blank=True)
    dataset = models.ForeignKey(Dataset, on_delete=models.SET_NULL, null=True, blank=True, related_name='patients')
    modalities = models.ManyToManyField(Modality, blank=True, related_name='patients', help_text='Modalities available for this patient')
    folder = models.ForeignKey('Folder', on_delete=models.SET_NULL, null=True, blank=True, related_name='patients')
    tags = models.ManyToManyField('Tag', blank=True, related_name='patients')
    
    visibility = models.CharField(max_length=10, choices=VISIBILITY_CHOICES, default='private')
    deleted = models.BooleanField(default=False, db_index=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

    objects = ActivePatientManager()
    all_objects = models.Manager()
    
    def tag_names(self):
        return list(self.tags.values_list('name', flat=True))

    def save(self, *args, **kwargs):
        creating = self._state.adding

        # First save to ensure patient_id is assigned
        super().save(*args, **kwargs)

        # After obtaining an ID, set default name if missing
        if creating and (self.name is None or self.name.strip() == ""):
            self.name = f"Patient {self.patient_id}"
            super().save(update_fields=['name'])
    
    def __str__(self):
        return f"Patient {self.patient_id} - {self.name}"

    @property
    def project(self):
        from common.models import Project

        return Project.objects.filter(slug='maxillo').first() or Project.objects.filter(name__iexact='maxillo').first()

    @property
    def project_id(self):
        project = self.project
        return project.id if project else None
    
    class Meta:
        indexes = [
            models.Index(fields=['visibility']),
            models.Index(fields=['uploaded_at']),
            models.Index(fields=['folder']),
            models.Index(fields=['name']),
            models.Index(fields=['visibility', 'uploaded_at']),
            models.Index(fields=['folder', 'visibility']),
        ]
        ordering = ['-uploaded_at']
    
    def has_ios_scans(self):
        """Check if both upper and lower scans are uploaded"""
        # Check FileRegistry for new processing flow
        try:
            # Check for both raw and processed files
            upper_raw = self.files.filter(file_type='ios_raw_upper').exists()
            lower_raw = self.files.filter(file_type='ios_raw_lower').exists()
            upper_processed = self.files.filter(file_type='ios_processed_upper').exists()
            lower_processed = self.files.filter(file_type='ios_processed_lower').exists()
            
            # Return True if we have either raw or processed files for both upper and lower
            return (upper_raw or upper_processed) and (lower_raw or lower_processed)
        except Exception as e:
            logger.error(f"Error checking IOS files for patient {self.patient_id}: {e}", exc_info=True)
            return False
        
    def has_cbct_scan(self):
        """Check if CBCT scan is uploaded"""
        # Check FileRegistry for new processing flow
        try:
            # Check for both raw and processed CBCT files
            has_raw = self.files.filter(file_type='cbct_raw').exists()
            has_processed = self.files.filter(file_type='cbct_processed').exists()
            return has_raw or has_processed
        except Exception as e:
            logger.error(f"Error checking CBCT files for patient {self.patient_id}: {e}", exc_info=True)
            return False
        
    def _processing_status(self, modality_slug):
        job = self.jobs.filter(modality_slug=modality_slug).order_by('-created_at').first()
        if not job:
            return 'not_uploaded'
        if job.status in ('pending', 'processing', 'retrying'):
            return 'processing'
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
        """Check if IOS processing is complete"""
        return self.ios_job_status == 'processed'

    def is_cbct_processed(self):
        """Check if CBCT processing is complete"""
        return self.cbct_job_status == 'processed'
    
    # New methods for working with FileRegistry system
    def has_rgb_images(self):
        """Check if patient has any RGB images uploaded"""
        try:
            return self.files.filter(file_type='rgb_image').exists()
        except Exception as e:
            logger.error(f"Error checking RGB files for patient {self.patient_id}: {e}", exc_info=True)
            return False

    def get_rgb_images(self):
        """Return queryset of RGB image FileRegistry entries for this patient"""
        return self.files.filter(file_type='rgb_image').order_by('-created_at')

    def get_raw_files(self):
        """Get all raw files from FileRegistry"""
        return self.files.filter(
            file_type__in=['cbct_raw', 'ios_raw_upper', 'ios_raw_lower', 'audio_raw']
        )
    
    def get_processed_files(self):
        """Get all processed files from FileRegistry"""
        return self.files.filter(
            file_type__in=['cbct_processed', 'ios_processed_upper', 'ios_processed_lower', 'audio_processed']
        )
    
    def get_cbct_raw_file(self):
        """Get CBCT raw file from FileRegistry"""
        try:
            return self.files.get(file_type='cbct_raw')
        except FileRegistry.DoesNotExist:
            return None
    
    def get_cbct_processed_file(self):
        """Get CBCT processed file from FileRegistry"""
        try:
            return self.files.get(file_type='cbct_processed')
        except FileRegistry.DoesNotExist:
            return None
    
    def get_ios_raw_files(self):
        """Get IOS raw files from FileRegistry"""
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
        """Get IOS processed files from FileRegistry"""
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
    
    def has_ios_scans_new(self):
        """Check if both upper and lower scans are available in FileRegistry"""
        ios_files = self.get_ios_raw_files()
        return ios_files['upper'] is not None and ios_files['lower'] is not None
        
    def has_cbct_scan_new(self):
        """Check if CBCT scan is available in FileRegistry"""
        return self.get_cbct_raw_file() is not None
    
    def get_pending_jobs(self):
        """Get pending processing jobs for this patient"""
        return self.processing_jobs.filter(status__in=['pending', 'processing', 'retrying'])
    
    def get_completed_jobs(self):
        """Get completed processing jobs for this patient"""
        return self.processing_jobs.filter(status='completed')
    
    def get_failed_jobs(self):
        """Get failed processing jobs for this patient"""
        return self.processing_jobs.filter(status='failed')
    
    def get_jobs_by_status(self, status):
        """Get jobs by specific status"""
        return self.processing_jobs.filter(status=status)
    
    def get_dependency_jobs(self):
        """Get jobs waiting for dependencies"""
        return self.processing_jobs.filter(status='dependency')
    
    def create_bite_classification_job(self, ios_job):
        """Create a bite classification job that depends on the IOS processing job"""
        from .models import Job
        
        existing_job = self.jobs.filter(modality_slug='bite_classification').first()
        if existing_job:
            return existing_job
        
        bite_job = Job.objects.create(
            modality_slug='bite_classification',
            status='dependency',
            patient=self,
            input_file_path='',
        )
        
        bite_job.add_dependency(ios_job)
        
        return bite_job


class IntraoralToothSegmentation(models.Model):
    """Vector tooth polygons per intraoral image."""

    patient = models.ForeignKey(
        Patient,
        on_delete=models.CASCADE,
        related_name='intraoral_segmentations',
    )
    image_file = models.ForeignKey(
        FileRegistry,
        on_delete=models.CASCADE,
        related_name='intraoral_segmentations',
    )
    teeth = models.JSONField(
        default=dict,
        blank=True,
        help_text='Map FDI tooth code to polygon sets [[[x, y], ...], ...] in image coordinates.',
    )
    is_confirmed = models.BooleanField(
        default=False,
        help_text='True when this image segmentation is reviewed and locked.',
    )
    confirmed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='confirmed_intraoral_segmentations',
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='updated_intraoral_segmentations',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['patient_id', 'image_file_id']
        constraints = [
            models.UniqueConstraint(
                fields=['patient', 'image_file'],
                name='uniq_maxillo_seg_patient_image',
            )
        ]
        indexes = [
            models.Index(fields=['patient', 'updated_at'], name='maxillo_int_patient_d8b901_idx'),
            models.Index(fields=['image_file'], name='maxillo_int_image_f_d24f72_idx'),
        ]

    def __str__(self):
        return f"IntraoralSegmentation {self.patient_id}:{self.image_file_id}"


class Classification(models.Model):
    CLASSIFIER_CHOICES = [
        ('manual', 'Manual'),
        ('pipeline', 'Pipeline'),
    ]
    
    SAGITTAL_CHOICES = [
        ('Unknown', 'Unknown'),
        ('I', 'Class I'),
        ('II_edge', 'Class II Edge'),
        ('II_full', 'Class II Full'),
        ('III', 'Class III'),
    ]
    
    VERTICAL_CHOICES = [
        ('Unknown', 'Unknown'),
        ('normal', 'Normal'),
        ('deep', 'Deep Bite'),
        ('reverse', 'Reverse Bite'),
        ('open', 'Open Bite'),
    ]
    
    TRANSVERSE_CHOICES = [
        ('Unknown', 'Unknown'),
        ('normal', 'Normal'),
        ('cross', 'Cross Bite'),
        ('scissor', 'Scissor Bite'),
    ]
    
    MIDLINE_CHOICES = [
        ('Unknown', 'Unknown'),
        ('centered', 'Centered'),
        ('deviated', 'Deviated'),
    ]
    
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='classifications', null=True, blank=True)
    classifier = models.CharField(max_length=10, choices=CLASSIFIER_CHOICES)
    
    sagittal_left = models.CharField(max_length=10, choices=SAGITTAL_CHOICES)
    sagittal_right = models.CharField(max_length=10, choices=SAGITTAL_CHOICES)
    vertical = models.CharField(max_length=10, choices=VERTICAL_CHOICES)
    transverse = models.CharField(max_length=10, choices=TRANSVERSE_CHOICES)
    midline = models.CharField(max_length=10, choices=MIDLINE_CHOICES)
    
    annotator = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['patient', 'classifier']),
            models.Index(fields=['classifier']),
        ]
    
    def __str__(self):
        return f"Classification {self.id} - {self.get_classifier_display()} - Patient {self.patient.patient_id}"


class VoiceCaption(models.Model):
    PROCESSING_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    
    # Modality is dynamic: accept any slug present in common.Modality
    # Keep as CharField to avoid breaking migrations and to allow flexible slugs
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='voice_captions', null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='voice_captions')
    modality = models.CharField(max_length=255, default='ios', help_text='Modality slug (e.g., ios, cbct, or any from common.Modality)')
    duration = models.FloatField(help_text='Duration of audio recording in seconds')
    text_caption = models.TextField(blank=True, null=True, help_text='Transcribed text from audio')
    original_text_caption = models.TextField(blank=True, null=True, help_text='Original transcription before any edits')
    is_edited = models.BooleanField(default=False, help_text='Whether the transcription has been manually edited')
    edit_history = models.JSONField(default=list, blank=True, help_text='History of edits with timestamps and users')
    processing_status = models.CharField(max_length=20, choices=PROCESSING_STATUS_CHOICES, default='pending', help_text='Status of speech-to-text processing')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['patient', 'processing_status']),
            models.Index(fields=['processing_status']),
            models.Index(fields=['user']),
        ]
    
    def get_modality_display(self):
        try:
            from common.models import Modality as _Modality
            if not self.modality:
                return 'Undefined'
            mod = _Modality.objects.filter(slug=self.modality).first()
            if mod:
                # Prefer label, then name, fallback to slug upper
                return getattr(mod, 'label', '') or getattr(mod, 'name', '') or self.modality.upper()
            return self.modality.upper()
        except Exception:
            # Fallback if common.Modality not available
            return (self.modality or 'Undefined').upper()
    
    def get_display_duration(self):
        """Return a human-readable duration string"""
        # For text-only captions (duration = 0), return "Text"
        if self.duration == 0:
            return "Text"
        
        minutes = int(self.duration // 60)
        seconds = int(self.duration % 60)
        if minutes > 0:
            return f"{minutes}:{seconds:02d}"
        return f"{seconds}s"
    
    def get_quality_status(self):
        """Return quality status based on duration or text content"""
        # For text-only captions (duration = 0), always return good quality
        if self.duration == 0:
            return {'color': 'success', 'message': 'Text'}
        
        if self.duration <= 30:
            return {'color': 'danger', 'message': 'Short'}
        elif self.duration <= 45:
            return {'color': 'warning', 'message': 'Good'}
        else:
            return {'color': 'success', 'message': 'Perfect'}
    
    def is_processed(self):
        """Check if speech-to-text processing is complete"""
        # For text-only captions (duration = 0), they are immediately processed
        if self.duration == 0:
            return self.processing_status == 'completed' and self.text_caption
        
        return self.processing_status == 'completed' and self.text_caption and self.text_caption != "[Audio processed but no transcription available]"
    
    def get_processing_display_text(self):
        """Get display text based on processing status"""
        if self.processing_status == 'completed':
            if self.text_caption and self.text_caption != "[Audio processed but no transcription available]":
                return self.text_caption
            else:
                return "[Audio processed but no transcription available]"
        elif self.processing_status == 'processing':
            return "Converting speech to text..."
        elif self.processing_status == 'failed':
            return "Processing failed"
        else:
            return "Preprocessing audio..."
    
    def get_display_text_caption(self):
        """Get the text caption to display, showing if it's edited"""
        if self.is_processed():
            text = self.text_caption
            if self.is_edited:
                text += " [edited]"
            return text
        return self.get_processing_display_text()
    
    def save_original_transcription(self):
        """Save the current transcription as original if not already set"""
        if self.text_caption and not self.original_text_caption:
            self.original_text_caption = self.text_caption
    
    def edit_transcription(self, new_text, user):
        """Edit the transcription and track the change"""
        if not self.is_processed():
            raise ValueError("Cannot edit transcription that is not yet processed")
        
        # Save original transcription if this is the first edit
        if not self.original_text_caption:
            self.original_text_caption = self.text_caption
        
        # Add to edit history
        edit_record = {
            'timestamp': timezone.now().isoformat(),
            'user_id': user.id,
            'username': user.username,
            'previous_text': self.text_caption,
            'new_text': new_text
        }
        
        if not self.edit_history:
            self.edit_history = []
        
        self.edit_history.append(edit_record)
        
        # Update the transcription
        self.text_caption = new_text
        self.is_edited = True
        self.save()
    
    def revert_to_original(self, user):
        """Revert transcription to the original version"""
        if not self.original_text_caption:
            raise ValueError("No original transcription to revert to")
        
        # Add revert action to edit history
        revert_record = {
            'timestamp': timezone.now().isoformat(),
            'user_id': user.id,
            'username': user.username,
            'action': 'reverted_to_original',
            'previous_text': self.text_caption,
            'reverted_text': self.original_text_caption
        }
        
        if not self.edit_history:
            self.edit_history = []
        
        self.edit_history.append(revert_record)
        
        # Revert the transcription
        self.text_caption = self.original_text_caption
        self.is_edited = False
        self.save()
    
    def get_audio_file(self):
        """Get audio file from FileRegistry"""
        try:
            return self.files.get(file_type='audio_raw')
        except FileRegistry.DoesNotExist:
            return None
    
    def get_processed_text_file(self):
        """Get processed text file from FileRegistry"""
        try:
            return self.files.get(file_type='audio_processed')
        except FileRegistry.DoesNotExist:
            return None
    
    def get_pending_jobs(self):
        """Get pending processing jobs for this voice caption"""
        return self.processing_jobs.filter(status__in=['pending', 'processing', 'retrying'])


## ProcessingJob moved to common.ProcessingJob


## FileRegistry moved to common.FileRegistry


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
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='exports')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    # Query parameters stored as JSON
    query_params = models.JSONField(default=dict, help_text='Stores folder_ids, modality_slugs, and filters')
    query_summary = models.CharField(max_length=500, blank=True, help_text='Human-readable query summary')
    
    # Export file information
    file_path = models.CharField(max_length=1000, blank=True, help_text='Path to generated ZIP file')
    file_size = models.BigIntegerField(default=0, help_text='Size of export file in bytes')
    patient_count = models.IntegerField(default=0, help_text='Number of patients in export')
    
    # Timing
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True, help_text='When processing started')
    completed_at = models.DateTimeField(null=True, blank=True, help_text='When processing completed')
    
    # Error handling
    error_message = models.TextField(blank=True, help_text='Error message if export failed')

    # Sharing
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

    # Live progress (during processing)
    progress_message = models.CharField(max_length=255, blank=True, help_text='Current phase or progress text')
    progress_percent = models.IntegerField(null=True, blank=True, help_text='Progress 0-100')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['status', 'created_at']),
        ]
        db_table = 'maxillo_export'
    
    def __str__(self):
        return f"Export {self.id} - {self.get_status_display()} - {self.user.username} - {self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else 'N/A'}"
    
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

    
