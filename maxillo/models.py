import secrets
import uuid

from django.db import models
from django.db.models import Q
from django.contrib.auth.models import User
import os
from django.utils import timezone
from django.utils.text import slugify
from common.models import Modality, Project, ProjectAccess, Job, FileRegistry, Invitation
from common.base_models import (
    ActivePatientManager,
    DatasetBase,
    FolderAccessBase,
    FolderBase,
    TagBase,
    ClassificationBase,
    ExportBase,
    VoiceCaptionBase,
)
import logging
logger = logging.getLogger(__name__)


# MaxilloUserProfile has been removed - user permissions are now handled via common.ProjectAccess
# Users get ProjectAccess entries when they accept an invitation or are granted access by an admin


class Dataset(DatasetBase):
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

    def scan_count(self):
        return self.patients.count()

    def patient_count(self):
        return self.patients.count()


class MaxilloProject(Project):
    """Project proxy bound to the maxillo domain.

    Lets Django admin show maxillo projects under the Maxillo section (the
    concrete Project model lives in common, so without a proxy it would appear
    under Common). The admin forces ``domain='maxillo'`` on create/change.
    """

    class Meta:
        proxy = True
        verbose_name = 'Maxillo project'
        verbose_name_plural = 'Maxillo projects'


class Folder(FolderBase):
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    # Mandatory project scope: folders live inside a Project (top-level folders
    # became Projects during the folder->project migration; remaining folders
    # are flat within a project). Non-null since migration 0028: while it was
    # nullable, every ModelForm -- the Django admin's "add folder" page included
    # -- treated it as optional, and a project-less folder is invisible to every
    # project-scoped listing while still being reachable by id.
    project = models.ForeignKey(
        'common.Project', on_delete=models.CASCADE,
        related_name='maxillo_folders',
    )

    class Meta:
        unique_together = ('project', 'name', 'parent')
        ordering = ['name']
        indexes = [
            models.Index(fields=['project']),
            models.Index(fields=['parent']),
            models.Index(fields=['name']),
        ]


class FolderAccess(FolderAccessBase):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='maxillo_folder_access')

    class Meta:
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
        ordering = ['name']
        indexes = [
            models.Index(fields=['name']),
        ]


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
    # Mandatory project scope (backfilled by the folder->project migration,
    # required since 0029). `folder` stays optional because its on_delete is
    # SET_NULL: deleting a folder legitimately leaves its patients folder-less.
    project = models.ForeignKey(
        'common.Project', on_delete=models.CASCADE,
        related_name='maxillo_patients',
    )
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

    class Meta:
        indexes = [
            models.Index(fields=['visibility']),
            models.Index(fields=['uploaded_at']),
            models.Index(fields=['folder']),
            models.Index(fields=['project']),
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
            processed = self.get_ios_processed_files()
            upper_processed = processed['upper'] is not None
            lower_processed = processed['lower'] is not None
            
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
            file_type__in=[
                'cbct_processed', 'ios_processed_upper', 'ios_processed_lower',
                'ios_processed', 'audio_processed',
            ]
        )
    
    def get_cbct_raw_file(self):
        """Get CBCT raw file from FileRegistry"""
        try:
            return self.files.get(file_type='cbct_raw')
        except FileRegistry.DoesNotExist:
            return None
    
    def get_cbct_processed_file(self):
        """Get CBCT processed file from FileRegistry"""
        return self.files.filter(file_type='cbct_processed').order_by(
            '-created_at', '-id'
        ).first()
    
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
        """Get IOS processed files from FileRegistry.

        Legacy rows are file_type='ios_processed_upper'/'_lower'; new completions
        write file_type='ios_processed'. Runner output filenames are preserved in
        subtype, so both 'upper' and names such as 'upper_oriented.stl' match.
        """
        arches = {'upper': None, 'lower': None}
        files = self.files.filter(
            file_type__in=['ios_processed_upper', 'ios_processed_lower', 'ios_processed']
        ).order_by('-created_at', '-id')
        for file_obj in files:
            arch = None
            if file_obj.file_type == 'ios_processed_upper':
                arch = 'upper'
            elif file_obj.file_type == 'ios_processed_lower':
                arch = 'lower'
            else:
                name = os.path.splitext(os.path.basename(file_obj.subtype.lower()))[0]
                tokens = name.replace('-', '_').split('_')
                if 'upper' in tokens:
                    arch = 'upper'
                elif 'lower' in tokens:
                    arch = 'lower'
            if arch and arches[arch] is None:
                arches[arch] = file_obj
            if arches['upper'] and arches['lower']:
                break
        return arches
    
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


class PanoramicState(models.Model):
    """Current browser-generated panoramic annotation and immutable outputs."""

    MODE_CHOICES = [('mip', 'Maximum intensity projection'), ('raysum', 'Ray sum')]
    GEOMETRY_SOURCE_CHOICES = [('auto', 'Automatic'), ('custom_cp', 'Edited')]

    patient = models.OneToOneField(
        Patient,
        on_delete=models.CASCADE,
        related_name='panoramic_state',
    )
    source_job = models.ForeignKey(
        Job,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='browser_panoramic_states',
    )
    source_file = models.ForeignKey(
        FileRegistry,
        on_delete=models.SET_NULL,
        null=True,
        related_name='browser_panoramic_sources',
    )
    source_file_key = models.CharField(max_length=60)
    source_file_hash = models.CharField(max_length=64)
    source_segmentation_file = models.ForeignKey(
        FileRegistry,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='browser_panoramic_segmentation_sources',
    )
    source_segmentation_key = models.CharField(max_length=60, blank=True)
    source_segmentation_hash = models.CharField(max_length=64, blank=True)
    mip_file = models.ForeignKey(
        FileRegistry,
        on_delete=models.SET_NULL,
        null=True,
        related_name='browser_panoramic_mip_states',
    )
    raysum_file = models.ForeignKey(
        FileRegistry,
        on_delete=models.SET_NULL,
        null=True,
        related_name='browser_panoramic_raysum_states',
    )
    axial_slice = models.PositiveIntegerField()
    volume_shape = models.JSONField()
    spline = models.JSONField()
    geometry_source = models.CharField(
        max_length=10,
        choices=GEOMETRY_SOURCE_CHOICES,
        default='custom_cp',
    )
    default_mode = models.CharField(max_length=10, choices=MODE_CHOICES)
    algorithm_version = models.CharField(max_length=32, default='panorex-js-v2-mip')
    revision = models.PositiveIntegerField(default=1)
    generation_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    request_hash = models.CharField(max_length=64)
    generated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='generated_panoramic_states',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"PanoramicState {self.patient_id} r{self.revision}"


class Classification(ClassificationBase):
    SAGITTAL_CHOICES = [
        ('Unknown', 'Unknown'),
        ('I', 'Class I'),
        # Plain Class II: what an automated classifier can state when it does not
        # model the edge/full distinction a human annotator makes (Bits2Bites
        # predicts a single "seconda classe"). Human annotation keeps using
        # II_edge/II_full.
        ('II', 'Class II'),
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

    classifier = models.CharField(max_length=10, choices=ClassificationBase.CLASSIFIER_CHOICES)
    sagittal_left = models.CharField(max_length=10, choices=SAGITTAL_CHOICES)
    sagittal_right = models.CharField(max_length=10, choices=SAGITTAL_CHOICES)
    vertical = models.CharField(max_length=10, choices=VERTICAL_CHOICES)
    transverse = models.CharField(max_length=10, choices=TRANSVERSE_CHOICES)
    midline = models.CharField(max_length=10, choices=MIDLINE_CHOICES)
    annotator = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['patient', 'classifier']),
            models.Index(fields=['classifier']),
        ]
        constraints = [
            # One classification per patient per classifier: the manual one a human
            # entered, and the pipeline one the model produced. Every write site treats
            # it that way (two use get_or_create, the accept-AI path uses
            # update_or_create), and duplicates only ever arrived from concurrent
            # writes -- where the older row silently shadowed the newer in
            # ``-timestamp`` order, so the UI showed a stale classification.
            models.UniqueConstraint(
                fields=('patient', 'classifier'),
                name='uniq_maxillo_class_patient_classifier',
            ),
        ]

    def __str__(self):
        return f"Classification {self.id} - {self.get_classifier_display()} - Patient {self.patient.patient_id}"


class VoiceCaption(VoiceCaptionBase):
    # Modality is dynamic: accept any slug present in common.Modality
    # Keep as CharField to avoid breaking migrations and to allow flexible slugs
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='voice_captions')
    modality = models.CharField(max_length=255, default='ios', help_text='Modality slug (e.g., ios, cbct, or any from common.Modality)')
    text_caption = models.TextField(blank=True, null=True, help_text='Transcribed text from audio')
    original_text_caption = models.TextField(blank=True, null=True, help_text='Original transcription before any edits')
    is_edited = models.BooleanField(default=False, help_text='Whether the transcription has been manually edited')
    edit_history = models.JSONField(default=list, blank=True, help_text='History of edits with timestamps and users')
    processing_status = models.CharField(max_length=20, choices=VoiceCaptionBase.PROCESSING_STATUS_CHOICES, default='pending', help_text='Status of speech-to-text processing')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['patient', 'processing_status']),
            models.Index(fields=['processing_status']),
            models.Index(fields=['user']),
        ]


## ProcessingJob moved to common.ProcessingJob


## FileRegistry moved to common.FileRegistry


class Export(ExportBase):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='exports')
    query_params = models.JSONField(default=dict, help_text='Stores folder_ids, modality_slugs, and filters')
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
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['status', 'created_at']),
        ]
        db_table = 'maxillo_export'

    def __str__(self):
        return f"Export {self.id} - {self.get_status_display()} - {self.user.username} - {self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else 'N/A'}"
