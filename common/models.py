from django.db import models
from django.contrib.auth.models import User
from django.utils.text import slugify


class Project(models.Model):
	name = models.CharField(max_length=50, unique=True)
	slug = models.SlugField(max_length=60, unique=True, blank=True)
	description = models.TextField(blank=True)
	icon = models.CharField(max_length=100, blank=True)
	is_active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
	modalities = models.ManyToManyField('Modality', blank=True, related_name='projects')

	class Meta:
		ordering = ['name']

	def __str__(self):
		return self.name

	def save(self, *args, **kwargs):
		if not self.slug:
			self.slug = slugify(self.name)
		super().save(*args, **kwargs)


class Modality(models.Model):
	name = models.CharField(max_length=50, unique=True)
	slug = models.SlugField(max_length=60, unique=True, blank=True)
	description = models.TextField(blank=True)
	# Optional icon CSS class for UI (e.g., 'fas fa-cube', 'fas fa-tooth')
	icon = models.CharField(max_length=100, blank=True)
	# Optional short UI label used when no icon is provided (e.g., 'F', 'T1')
	label = models.CharField(max_length=20, blank=True)
	supported_extensions = models.JSONField(default=list)
	# Optional list of subtypes (e.g., for IOS: ["upper", "lower"]).
	# Allows per-modality subtype toggles and FileRegistry mapping.
	subtypes = models.JSONField(default=list, blank=True)
	requires_multiple_files = models.BooleanField(default=False)
	is_active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

	class Meta:
		ordering = ['name']

	def __str__(self):
		return self.name

	def save(self, *args, **kwargs):
		if not self.slug:
			self.slug = slugify(self.name)
		super().save(*args, **kwargs)


class ProjectAccess(models.Model):
	ROLE_CHOICES = [
		('standard', 'Standard User'),
		('annotator', 'Annotator'),
		('project_manager', 'Project Manager'),
		('admin', 'Administrator'),
		('student_dev', 'Student Developer'),
	]

	user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='project_access')
	project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='access_list')
	role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='standard')
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		unique_together = ('user', 'project')

	def __str__(self):
		return f"{self.user.username} -> {self.project.name}"

	def is_annotator(self):
		return self.role in ['annotator', 'project_manager', 'admin']

	def is_project_manager(self):
		return self.role == 'project_manager'

	def is_admin(self):
		return self.role == 'admin'

	def is_student_developer(self):
		return self.role == 'student_dev'

	def can_upload_scans(self):
		return self.role in ['annotator', 'project_manager', 'admin', 'student_dev']

	def can_see_debug_scans(self):
		return self.role in ['admin', 'student_dev']

	def can_see_public_private_scans(self):
		return self.role in ['annotator', 'project_manager', 'admin', 'standard']

	def can_modify_scan_settings(self):
		return self.role in ['annotator', 'project_manager', 'admin']

	def can_delete_scans(self):
		return self.role == 'admin'

	def can_delete_debug_scans(self):
		return self.role in ['admin', 'student_dev']

	def can_view_other_profiles(self):
		return self.role in ['project_manager', 'admin']

	def get_role_display(self):
		return dict(self.ROLE_CHOICES).get(self.role, self.role)


# Shared models used by all apps. These map to existing 'scans_*' tables.


class Invitation(models.Model):
	ROLE_CHOICES = [
		('standard', 'Standard User'),
		('annotator', 'Annotator'),
		('project_manager', 'Project Manager'),
		('admin', 'Administrator'),
		('student_dev', 'Student Developer'),
	]

	code = models.CharField(max_length=64, unique=True)
	email = models.EmailField(blank=True, null=True)
	role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='standard')
	projects = models.ManyToManyField(Project, related_name='invitations_multi', help_text='Projects the user will have access to')
	project = models.ForeignKey(Project, on_delete=models.CASCADE, null=False, blank=False, related_name='invitations', help_text='Project the user will have access to')
	created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
	created_at = models.DateTimeField(auto_now_add=True)
	expires_at = models.DateTimeField()
	email_sent_at = models.DateTimeField(null=True, blank=True)
	email_send_error = models.TextField(blank=True, null=True)
	used_at = models.DateTimeField(null=True, blank=True)
	used_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='used_invitation')

	def is_valid(self):
		"""Check if invitation is still valid (not expired and not used)."""
		from django.utils import timezone
		return self.used_at is None and self.expires_at > timezone.now()

	def __str__(self):
		project_count = self.projects.count() if self.pk else 0
		if project_count == 1:
			project_str = f" - {self.projects.first().name}"
		elif project_count > 1:
			project_str = f" - {project_count} projects"
		else:
			project_str = f" - {self.project.name}" if self.project else ""
		return f"Invitation {self.code} - {self.role}{project_str}"

	class Meta:
		db_table = 'maxillo_invitation'


class Job(models.Model):
	DOMAIN_CHOICES = [
		('maxillo', 'Maxillo'),
		('brain', 'Brain'),
	]

	STATUS_CHOICES = [
		('pending', 'Pending'),
		('dependency', 'Waiting for Dependencies'),
		('processing', 'Processing'),
		('completed', 'Completed'),
		('failed', 'Failed'),
		('retrying', 'Retrying'),
	]

	modality_slug = models.CharField(max_length=60, help_text='Slug for modality (e.g., cbct, ios, audio, bite_classification)')
	status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
	priority = models.IntegerField(default=0, help_text='Higher values = higher priority')
	dependencies = models.ManyToManyField('self', blank=True, symmetrical=False, related_name='dependent_jobs', help_text='Jobs that must complete before this job can start')
	domain = models.CharField(max_length=20, choices=DOMAIN_CHOICES, default='maxillo')
	patient = models.ForeignKey('maxillo.Patient', on_delete=models.CASCADE, related_name='jobs', null=True, blank=True)
	brain_patient = models.ForeignKey('brain.Patient', on_delete=models.CASCADE, related_name='jobs', null=True, blank=True)
	voice_caption = models.ForeignKey('maxillo.VoiceCaption', on_delete=models.CASCADE, related_name='jobs', null=True, blank=True)
	brain_voice_caption = models.ForeignKey('brain.VoiceCaption', on_delete=models.CASCADE, related_name='jobs', null=True, blank=True)

	# IO
	input_file_path = models.CharField(max_length=500, help_text='Primary input object key', blank=True)
	output_files = models.JSONField(default=dict, blank=True, help_text='Dict of output file paths and metadata')

	# Timing and metadata
	created_at = models.DateTimeField(auto_now_add=True)
	started_at = models.DateTimeField(null=True, blank=True)
	completed_at = models.DateTimeField(null=True, blank=True)

	# Error handling
	retry_count = models.IntegerField(default=0)
	max_retries = models.IntegerField(default=3)
	error_logs = models.TextField(blank=True, help_text='Error logs if processing failed')

	# Worker info (generic, non-Docker-specific)
	worker_id = models.CharField(max_length=100, blank=True, help_text='ID of worker processing this job')

	class Meta:
		ordering = ['-priority', 'created_at']
		indexes = [
			models.Index(fields=['domain', 'status', 'created_at']),
			models.Index(fields=['domain', 'modality_slug', 'status']),
			models.Index(fields=['modality_slug', 'status']),
			models.Index(fields=['status', 'created_at']),
			models.Index(fields=['patient', 'modality_slug', 'status']),  # Optimize patient list queries
		]
		db_table = 'maxillo_job'

	def __str__(self):
		related_obj = self.patient or self.brain_patient or self.voice_caption or self.brain_voice_caption
		return f"Job {self.id} - {self.modality_slug} - {self.get_status_display()} - {related_obj}"

	def can_retry(self):
		return self.status == 'failed' and self.retry_count < self.max_retries

	def mark_processing(self, worker_id=None):
		self.status = 'processing'
		from django.utils import timezone as _tz
		self.started_at = _tz.now()
		if worker_id:
			self.worker_id = worker_id
		self.save()

	def mark_completed(self, output_files=None):
		self.status = 'completed'
		from django.utils import timezone as _tz
		self.completed_at = _tz.now()
		if output_files:
			self.output_files = output_files
		self.save()
		self.notify_dependents()

	@classmethod
	def get_ready_jobs(cls):
		return cls.objects.filter(status='pending').order_by('-priority', 'created_at')

	@classmethod
	def get_dependency_jobs(cls):
		return cls.objects.filter(status='dependency')

	def add_dependency(self, dependency_job):
		self.dependencies.add(dependency_job)
		self.update_status_based_on_dependencies()

	def remove_dependency(self, dependency_job):
		self.dependencies.remove(dependency_job)
		self.update_status_based_on_dependencies()

	def get_dependent_jobs(self):
		return self.dependent_jobs.all()

	def notify_dependents(self):
		for dependent in self.dependent_jobs.all():
			dependent.update_status_based_on_dependencies()

	def check_dependencies(self):
		if not self.dependencies.exists():
			return True
		return all(dep.status == 'completed' for dep in self.dependencies.all())

	def update_status_based_on_dependencies(self):
		if self.status == 'dependency' and self.check_dependencies():
			self.status = 'pending'
			self.save()
			return True
		elif self.status == 'pending' and not self.check_dependencies():
			self.status = 'dependency'
			self.save()
			return True
		return False

	def mark_failed(self, error_msg, can_retry=True):
		self.error_logs = error_msg
		if can_retry and self.can_retry():
			self.status = 'retrying'
			self.retry_count += 1
		else:
			self.status = 'failed'
		self.save()

	def get_processing_duration(self):
		if self.started_at and self.completed_at:
			return self.completed_at - self.started_at
		return None

class ProcessingJob(models.Model):
	DOMAIN_CHOICES = [
		('maxillo', 'Maxillo'),
		('brain', 'Brain'),
	]

	JOB_TYPE_CHOICES = [
		('cbct', 'CBCT Processing'),
		('ios', 'IOS Processing'),
		('audio', 'Audio Speech-to-Text'),
		('bite_classification', 'Bite Classification'),
	]

	JOB_STATUS_CHOICES = [
		('pending', 'Pending'),
		('dependency', 'Waiting for Dependencies'),
		('processing', 'Processing'),
		('completed', 'Completed'),
		('failed', 'Failed'),
		('retrying', 'Retrying'),
	]

	job_type = models.CharField(max_length=20, choices=JOB_TYPE_CHOICES)
	status = models.CharField(max_length=20, choices=JOB_STATUS_CHOICES, default='pending')
	priority = models.IntegerField(default=0, help_text='Higher values = higher priority')
	dependencies = models.ManyToManyField('self', blank=True, symmetrical=False, related_name='dependent_jobs', help_text='Jobs that must complete before this job can start')
	domain = models.CharField(max_length=20, choices=DOMAIN_CHOICES, default='maxillo')
	patient = models.ForeignKey('maxillo.Patient', on_delete=models.CASCADE, related_name='processing_jobs', null=True, blank=True)
	brain_patient = models.ForeignKey('brain.Patient', on_delete=models.CASCADE, related_name='processing_jobs', null=True, blank=True)
	voice_caption = models.ForeignKey('maxillo.VoiceCaption', on_delete=models.CASCADE, related_name='processing_jobs', null=True, blank=True)
	brain_voice_caption = models.ForeignKey('brain.VoiceCaption', on_delete=models.CASCADE, related_name='processing_jobs', null=True, blank=True)

	# File paths
	input_file_path = models.CharField(max_length=500, help_text='Input object key')
	output_files = models.JSONField(default=dict, blank=True, help_text='Dict of output file paths and metadata')

	# Processing info
	docker_image = models.CharField(max_length=200, help_text='Docker image used for processing')
	docker_command = models.JSONField(default=list, help_text='Docker command arguments')

	# Timing and metadata
	created_at = models.DateTimeField(auto_now_add=True)
	started_at = models.DateTimeField(null=True, blank=True)
	completed_at = models.DateTimeField(null=True, blank=True)

	# Error handling
	retry_count = models.IntegerField(default=0)
	max_retries = models.IntegerField(default=3)
	error_logs = models.TextField(blank=True, help_text='Error logs if processing failed')

	# Worker info
	worker_id = models.CharField(max_length=100, blank=True, help_text='ID of worker processing this job')

	class Meta:
		ordering = ['-priority', 'created_at']
		indexes = [
			models.Index(fields=['domain', 'status', 'created_at']),
			models.Index(fields=['domain', 'job_type', 'status']),
			models.Index(fields=['job_type', 'status']),
			models.Index(fields=['status', 'created_at']),
		]
		db_table = 'maxillo_processingjob'

	def __str__(self):
		related_obj = self.patient or self.brain_patient or self.voice_caption or self.brain_voice_caption
		return f"ProcessingJob {self.id} - {self.get_job_type_display()} - {self.get_status_display()} - {related_obj}"

	def can_retry(self):
		return self.status == 'failed' and self.retry_count < self.max_retries

	def mark_processing(self, worker_id=None):
		self.status = 'processing'
		from django.utils import timezone as _tz
		self.started_at = _tz.now()
		if worker_id:
			self.worker_id = worker_id
		self.save()

	def mark_completed(self, output_files=None):
		self.status = 'completed'
		from django.utils import timezone as _tz
		self.completed_at = _tz.now()
		if output_files:
			self.output_files = output_files
		self.save()
		self.notify_dependents()

	@classmethod
	def get_ready_jobs(cls):
		return cls.objects.filter(status='pending').order_by('-priority', 'created_at')

	@classmethod
	def get_dependency_jobs(cls):
		return cls.objects.filter(status='dependency')

	def add_dependency(self, dependency_job):
		self.dependencies.add(dependency_job)
		self.update_status_based_on_dependencies()

	def remove_dependency(self, dependency_job):
		self.dependencies.remove(dependency_job)
		self.update_status_based_on_dependencies()

	def get_dependent_jobs(self):
		return self.dependent_jobs.all()

	def notify_dependents(self):
		for dependent in self.dependent_jobs.all():
			dependent.update_status_based_on_dependencies()

	def check_dependencies(self):
		if not self.dependencies.exists():
			return True
		return all(dep.status == 'completed' for dep in self.dependencies.all())

	def update_status_based_on_dependencies(self):
		if self.status == 'dependency' and self.check_dependencies():
			self.status = 'pending'
			self.save()
			return True
		elif self.status == 'pending' and not self.check_dependencies():
			self.status = 'dependency'
			self.save()
			return True
		return False

	def mark_failed(self, error_msg, can_retry=True):
		self.error_logs = error_msg
		if can_retry and self.can_retry():
			self.status = 'retrying'
			self.retry_count += 1
		else:
			self.status = 'failed'
		self.save()

	def get_processing_duration(self):
		if self.started_at and self.completed_at:
			return self.completed_at - self.started_at
		return None


class FileRegistry(models.Model):
	DOMAIN_CHOICES = [
		('maxillo', 'Maxillo'),
		('brain', 'Brain'),
	]

	FILE_TYPE_CHOICES = [
		('cbct_raw', 'CBCT Raw'),
		('cbct_processed', 'CBCT Processed'),
		('ios_raw_upper', 'IOS Raw Upper'),
		('ios_raw_lower', 'IOS Raw Lower'),
		('ios_processed_upper', 'IOS Processed Upper'),
		('ios_processed_lower', 'IOS Processed Lower'),
		('audio_raw', 'Audio Raw'),
		('audio_processed', 'Audio Processed Text'),
		('bite_classification', 'Bite Classification Results'),
		('rgb_image', 'RGB Image'),
		('volume_raw', 'Volume Raw'),
		('volume_processed', 'Volume Processed'),
		('image_raw', 'Image Raw'),
		('image_processed', 'Image Processed'),
		('generic_raw', 'Generic Raw'),
		('generic_processed', 'Generic Processed'),
		# Brain modalities
		('braintumor_mri_t1_raw', 'Brain MRI T1 Raw'),
		('braintumor_mri_t1_processed', 'Brain MRI T1 Processed'),
		('braintumor_mri_t1c_raw', 'Brain MRI T1c Raw'),
		('braintumor_mri_t1c_processed', 'Brain MRI T1c Processed'),
		('braintumor_mri_t2_raw', 'Brain MRI T2 Raw'),
		('braintumor_mri_t2_processed', 'Brain MRI T2 Processed'),
		('braintumor_mri_flair_raw', 'Brain MRI FLAIR Raw'),
		('braintumor_mri_flair_processed', 'Brain MRI FLAIR Processed'),
		# Maxillo image modalities
		('intraoral_raw', 'Intraoral Photographs Raw'),
		('intraoral_processed', 'Intraoral Photographs Processed'),
		('teleradiography_raw', 'Teleradiography Raw'),
		('teleradiography_processed', 'Teleradiography Processed'),
		('panoramic_raw', 'panoramic Raw'),
		('panoramic_processed', 'panoramic Processed'),
	]

	file_type = models.CharField(max_length=255, choices=FILE_TYPE_CHOICES)
	file_path = models.CharField(max_length=500, unique=True, help_text='Full path to file')
	file_size = models.BigIntegerField(help_text='File size in bytes')
	file_hash = models.CharField(max_length=64, help_text='SHA256 hash of file')
	# Dynamic modality linkage and optional subtype (e.g., 'upper', 'lower')
	modality = models.ForeignKey('Modality', on_delete=models.SET_NULL, null=True, blank=True, related_name='files')
	subtype = models.CharField(max_length=60, blank=True)
	domain = models.CharField(max_length=20, choices=DOMAIN_CHOICES, default='maxillo')
	patient = models.ForeignKey('maxillo.Patient', on_delete=models.CASCADE, related_name='files', null=True, blank=True)
	brain_patient = models.ForeignKey('brain.Patient', on_delete=models.CASCADE, related_name='files', null=True, blank=True)
	voice_caption = models.ForeignKey('maxillo.VoiceCaption', on_delete=models.CASCADE, related_name='files', null=True, blank=True)
	brain_voice_caption = models.ForeignKey('brain.VoiceCaption', on_delete=models.CASCADE, related_name='files', null=True, blank=True)
	processing_job = models.ForeignKey('common.Job', on_delete=models.CASCADE, related_name='files', null=True, blank=True)
	created_at = models.DateTimeField(auto_now_add=True)
	metadata = models.JSONField(default=dict, blank=True, help_text='Additional file metadata')

	class Meta:
		indexes = [
			models.Index(fields=['domain', 'file_type', 'created_at']),
			models.Index(fields=['domain', 'file_type', 'patient']),
			models.Index(fields=['domain', 'file_type', 'brain_patient']),
			models.Index(fields=['file_type', 'patient']),
			models.Index(fields=['modality', 'patient']),
			models.Index(fields=['modality', 'subtype', 'patient']),
			models.Index(fields=['file_path']),
		]
		db_table = 'maxillo_fileregistry'

	def __str__(self):
		return f"FileRegistry {self.id} - {self.file_type} - {self.file_path}"
	
	def get_file_type_display_name(self):
		"""
		Get the human-readable display name for the file type.
		If modality_name is available, use it; otherwise fall back to FILE_TYPE_CHOICES.
		"""
		# First check if we have a modality with a custom name
		if self.modality and hasattr(self.modality, 'name') and self.modality.name:
			return self.modality.name
		
		# Fall back to the choices mapping
		return self.get_file_type_display()
	
	@property
	def file_type_display_name(self):
		"""Property version of get_file_type_display_name for template use"""
		return self.get_file_type_display_name()
	
	@property
	def modality_name(self):
		"""Get modality name if available"""
		if self.modality and hasattr(self.modality, 'name'):
			return self.modality.name
		return None
	
	@classmethod
	def get_file_type_choices_dict(cls):
		"""
		Return FILE_TYPE_CHOICES as a dictionary for easy lookup.
		This can be used throughout the codebase for consistent file type display names.
		"""
		return dict(cls.FILE_TYPE_CHOICES)
	
	@classmethod
	def get_display_name_for_file_type(cls, file_type):
		"""
		Get display name for a given file type without needing a FileRegistry instance.
		Useful for programmatic access in views and utilities.
		"""
		choices_dict = cls.get_file_type_choices_dict()
		return choices_dict.get(file_type, file_type.replace('_', ' ').title())
