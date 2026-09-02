"""Shared cross-domain models (Job, ProcessingJob, FileRegistry, Project, ...).

DOMAIN_CHOICES and the per-domain FK field map now live in common.domains
(Phase 5.2) — single source of truth instead of the copies that used to be
duplicated across the models below.
"""
import uuid

from django.db import models
from django.contrib.auth.models import User
from django.utils.text import slugify

from common.domains import (
	DOMAIN_CHOICES,
	DOMAIN_FK_FIELDS,
	fk_fields_for,
	normalize_domain,
)


class DomainFKAccessorMixin:
	"""Registry-driven access to the per-domain patient / voice_caption FKs.

	Job, ProcessingJob and FileRegistry each carry three parallel patient FK
	columns (maxillo/brain/laparoscopy) plus three voice_caption FKs. These
	accessors wrap that fan-out so callers use ``obj.get_patient()`` /
	``obj.set_patient(p)`` instead of branching on ``obj.domain`` by hand.
	Methods only — no fields — so they add nothing to the migration state.
	"""

	def get_patient(self):
		patient_fk, _ = fk_fields_for(self.domain)
		return getattr(self, patient_fk, None)

	def get_voice_caption(self):
		_, voice_fk = fk_fields_for(self.domain)
		return getattr(self, voice_fk, None)

	def set_patient(self, patient):
		domain = normalize_domain(
			getattr(getattr(patient, "_meta", None), "app_label", self.domain)
		)
		self.domain = domain
		for slug, (patient_fk, _voice_fk) in DOMAIN_FK_FIELDS.items():
			setattr(self, patient_fk, patient if slug == domain else None)

	def set_voice_caption(self, voice_caption):
		patient = getattr(voice_caption, "patient", None)
		domain = normalize_domain(
			getattr(getattr(patient, "_meta", None), "app_label", self.domain)
		)
		self.domain = domain
		for slug, (_patient_fk, voice_fk) in DOMAIN_FK_FIELDS.items():
			setattr(self, voice_fk, voice_caption if slug == domain else None)


class Project(models.Model):
	# Unique per domain: two domains may both have a "Demo" project.
	name = models.CharField(max_length=50)
	slug = models.SlugField(max_length=60, unique=True, blank=True)
	description = models.TextField(blank=True)
	icon = models.CharField(max_length=100, blank=True)
	# Domain this project belongs to (maxillo/brain/laparoscopy). Projects are
	# scoped under one domain app: folders and patients inside the project live
	# in that domain's tables.
	domain = models.CharField(max_length=20, choices=DOMAIN_CHOICES, default='maxillo')
	is_active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	# blank=True as well as null=True: this is an audit column nobody fills in by
	# hand. Without it every ModelForm -- the admin's "add project" page, which
	# the control panel's "New project" button links to -- made it a required
	# picker and refused the form when it was left empty, so a project could not
	# be created from the admin at all. The admin fills it from the request.
	created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
	modalities = models.ManyToManyField('Modality', blank=True, related_name='projects')
	# Annotation methods enabled for this project. Empty = none; the UI hides
	# annotation tools whose method is not enabled here.
	annotation_methods = models.ManyToManyField('AnnotationMethod', blank=True, related_name='projects')
	# Processing steps explicitly disabled for this project. Empty = all steps
	# enabled (modulo the project's modality set); a step checked here never
	# dispatches a job for this project's patients (upload-time dispatch, rerun
	# picker and viewer availability all respect it).
	disabled_steps = models.ManyToManyField(
		'ProcessingStep', blank=True, related_name='disabled_for_projects'
	)

	class Meta:
		ordering = ['domain', 'name']
		unique_together = [('domain', 'name')]

	def __str__(self):
		return self.name

	def allows_annotation(self, method_slug):
		"""Whether an annotation method (e.g. 'ios_landmarks') is enabled."""
		return self.annotation_methods.filter(
			slug=method_slug, is_active=True
		).exists()

	def save(self, *args, **kwargs):
		if not self.slug:
			self.slug = slugify(self.name)
		super().save(*args, **kwargs)


class AnnotationMethod(models.Model):
	"""An annotation tool a Project may enable (e.g. 'ios_landmarks').

	``domain`` is blank for a method available in every domain (e.g. voice
	captions) or set to one domain slug for a domain-specific tool (e.g.
	'ios_landmarks' only makes sense under maxillo).
	"""
	name = models.CharField(max_length=100, unique=True)
	slug = models.SlugField(max_length=60, unique=True, blank=True)
	description = models.TextField(blank=True)
	icon = models.CharField(max_length=100, blank=True)
	domain = models.CharField(
		max_length=20, choices=DOMAIN_CHOICES, blank=True, default='',
		help_text="Leave blank for a method available in every domain.",
	)
	is_active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)
	# See Project.created_by: null=True without blank=True made the admin's add
	# form demand a value it exists to record automatically.
	created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

	class Meta:
		ordering = ['domain', 'name']

	def __str__(self):
		return self.name

	def save(self, *args, **kwargs):
		if not self.slug:
			self.slug = slugify(self.name)
		super().save(*args, **kwargs)


class Modality(models.Model):
	"""An imaging modality a Project may collect (e.g. 'cbct').

	``domain`` is blank for a modality available in every domain, or one domain
	slug for a domain-specific one -- 'cbct' only means anything under maxillo.
	Same field, same meaning and same blank-is-everywhere rule as
	:class:`AnnotationMethod`, so the admin scopes both with one filter.
	"""
	name = models.CharField(max_length=50, unique=True)
	slug = models.SlugField(max_length=60, unique=True, blank=True)
	description = models.TextField(blank=True)
	domain = models.CharField(
		max_length=20, choices=DOMAIN_CHOICES, blank=True, default='',
		help_text="Leave blank for a modality available in every domain.",
	)
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
	# See Project.created_by.
	created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

	class Meta:
		ordering = ['name']

	def __str__(self):
		return self.name

	def save(self, *args, **kwargs):
		if not self.slug:
			self.slug = slugify(self.name)
		super().save(*args, **kwargs)


class ProcessingStep(models.Model):
	"""Admin-declared processing step (one runner job) for a modality (Phase 4).

	A modality can have several steps forming a DAG: a step may declare
	prerequisite steps that must complete before it runs, and it receives their
	outputs as its input (see Job._pull_dependency_outputs). This replaces the
	single-flag ModalityProcessingConfig — a modality "needs processing" iff it
	has at least one enabled step, folding the old ``requires_processing`` and
	``is_enabled`` into one flag.

	``slug`` is the runner routing key carried on Job.modality_slug, so it is
	globally unique. The step whose slug equals its modality's slug is that
	modality's *root* step: an upload's source Job stands in for it, and every
	other step is wired (directly or transitively) downstream of a root.
	"""
	modality = models.ForeignKey(
		Modality, on_delete=models.CASCADE, related_name='steps'
	)
	name = models.CharField(max_length=100)
	# Runner routing key (e.g. 'ios', 'ios_orientation'); globally unique.
	slug = models.SlugField(max_length=60, unique=True)
	# Explicit queue override; when non-blank it wins over ALL env routing.
	queue_name = models.CharField(max_length=100, blank=True, default='')
	# SLURM-over-SSH dispatch opt-in (Yggdrasil 2.0). When non-blank, the runner
	# worker submits settings.ALGO_BASE_DIR/<algo_name>/run.sbatch on the cluster
	# login node for this step's jobs instead of enqueueing a Celery task; blank
	# keeps the historical Celery path. Resource requests (partition, gres, time,
	# ...) live as #SBATCH directives inside that run.sbatch, not here.
	algo_name = models.CharField(
		max_length=200, blank=True, default='',
		help_text="Exact algo directory name under ALGO_BASE_DIR on the cluster "
		"(e.g. 'sn' -> ALGO_BASE_DIR/sn/run.sbatch). Non-blank routes this step "
		"to SLURM-over-SSH dispatch instead of Celery.",
	)
	# Steps whose output feeds this step's input. Declaring one here is what
	# establishes the dependency: at upload time create_step_jobs wires a
	# Job.dependencies edge so this step waits for each input to complete, and
	# Job._pull_dependency_outputs merges each input's outputs into this step's
	# input_files. Self-referential DAG (may span modalities, e.g. bite->ios).
	depends_on = models.ManyToManyField(
		'self', symmetrical=False, blank=True, related_name='dependents',
		verbose_name='Inputs',
		help_text="Steps whose output feeds this step's input. Declaring an "
		"input automatically makes this step wait for it to complete.",
	)
	# Disabled steps create no runner Job (absorbs requires_processing + the old
	# per-modality is_enabled kill switch).
	is_enabled = models.BooleanField(default=True)
	# When True, an in-flight job for this step gates patient readiness (patient
	# shows 'processing') AND its modality's raw input files stay hidden/
	# un-downloadable until processing produces a *_processed file.
	is_blocking = models.BooleanField(default=True)
	# When True, this modality's raw input files are never listed or served in
	# the patient file view (a security screen). The files still exist in MySQL
	# and object storage — only visibility/download is blocked.
	discard_raw = models.BooleanField(default=False)
	prefer_processed_for_viewer = models.BooleanField(
		default=False,
		help_text="Prefer a complete processed file set in the modality viewer, "
		"falling back to raw files when processed files are unavailable.",
	)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ['modality__name', 'slug']

	def __str__(self):
		return f"{self.modality.slug}:{self.slug}"

	def clean(self):
		"""Refuse ``discard_raw`` for a modality whose raw input is DICOM.

		For a NIfTI modality, hiding the raw file is a security screen with no cost:
		the viewer shows the *processed* volume. For a DICOM one the raw series may be
		the only volume there is -- ``maxillo.views.patient_detail._usable_raw_volumes``
		falls back to it whenever processing produced no ``volume_nifti`` -- so setting
		this flag would blank the viewer for every patient on the modality at once,
		with the files still sitting in storage and nothing on screen explaining it.

		Roadmap risk 10. Guarded here *and* at ingest, because an admin can set the
		flag before any series exists and an upload can arrive after it was set.
		"""
		super().clean()
		if not self.discard_raw or not self.modality_id:
			return
		from django.core.exceptions import ValidationError as _ValidationError

		from common.dicom.models import DicomSeries

		if DicomSeries.objects.filter(file__modality_id=self.modality_id).exists():
			raise _ValidationError({
				"discard_raw": (
					"This modality has DICOM series stored natively, and for those the "
					"raw row is the volume the viewer displays. Hiding it would leave "
					"every one of these patients with an empty viewer."
				)
			})

	def save(self, *args, **kwargs):
		if not self.slug:
			self.slug = slugify(self.name)
		super().save(*args, **kwargs)


class UserSession(models.Model):
    """
    A reconstructed period of continuous activity for a user, built from
    presence heartbeats (see common.presence). A request occurring more
    than PRESENCE_TTL_SECONDS after the last heartbeat starts a new row
    instead of extending the previous one, so gaps naturally split sessions.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sessions")
    project_slug = models.CharField(max_length=50, blank=True, default="")
    started_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=["user", "-last_seen_at"]),
            models.Index(fields=["user", "project_slug", "-last_seen_at"]),
        ]
        ordering = ["-started_at"]

    @property
    def duration_seconds(self):
        return (self.last_seen_at - self.started_at).total_seconds()

    def __str__(self):
        return f"{self.user.username} {self.started_at} -> {self.last_seen_at}"


class ProjectAccess(models.Model):
	ROLE_CHOICES = [
		('viewer', 'Viewer'),
		('annotator', 'Annotator'),
		('admin', 'Administrator'),
	]

	user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='project_access')
	project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='access_list')
	role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='viewer')
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		unique_together = ('user', 'project')

	def __str__(self):
		return f"{self.user.username} -> {self.project.name}"

	def is_annotator(self):
		return self.role in ['annotator', 'admin']

	def is_project_manager(self):
		return False

	def is_admin(self):
		return self.role == 'admin'

	def is_student_developer(self):
		return False

	def can_upload_scans(self):
		return self.role in ['admin', 'annotator']

	def can_see_debug_scans(self):
		return self.role == 'admin'

	def can_see_public_private_scans(self):
		return True

	def can_modify_scan_settings(self):
		return self.role == 'admin'

	def can_delete_scans(self):
		return self.role == 'admin'

	def can_delete_debug_scans(self):
		return self.role == 'admin'

	def can_view_other_profiles(self):
		return self.role == 'admin'

	def get_role_display(self):
		return dict(self.ROLE_CHOICES).get(self.role, self.role)


# Shared models used by all apps. These map to existing 'scans_*' tables.


class Invitation(models.Model):
	ROLE_CHOICES = [
		('viewer', 'Viewer'),
		('annotator', 'Annotator'),
		('admin', 'Administrator'),
	]

	code = models.CharField(max_length=64, unique=True)
	email = models.EmailField(blank=True, null=True)
	role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='viewer')
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

	def save(self, *args, **kwargs):
		if not self.code:
			self.code = str(uuid.uuid4())
			update_fields = kwargs.get('update_fields')
			if update_fields is not None:
				kwargs['update_fields'] = set(update_fields) | {'code'}
		super().save(*args, **kwargs)

	class Meta:
		db_table = 'maxillo_invitation'


class Job(DomainFKAccessorMixin, models.Model):
	STATUS_CHOICES = [
		('pending', 'Pending'),
		('dependency', 'Waiting for Dependencies'),
		('processing', 'Processing'),
		('completed', 'Completed'),
		('failed', 'Failed'),
		('retrying', 'Retrying'),
	]

	modality_slug = models.CharField(max_length=60, help_text='Slug for modality (e.g., cbct, ios, audio, bite_classification)')
	# Pipeline step this job runs, when declared in admin. Null for legacy jobs
	# and jobs whose modality has no ProcessingStep rows.
	step = models.ForeignKey('common.ProcessingStep', on_delete=models.SET_NULL, null=True, blank=True, related_name='jobs')
	status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
	priority = models.IntegerField(default=0, help_text='Higher values = higher priority')
	dependencies = models.ManyToManyField('self', blank=True, symmetrical=False, related_name='dependent_jobs', help_text='Jobs that must complete before this job can start')
	domain = models.CharField(max_length=20, choices=DOMAIN_CHOICES, default='maxillo')
	patient = models.ForeignKey('maxillo.Patient', on_delete=models.CASCADE, related_name='jobs', null=True, blank=True)
	brain_patient = models.ForeignKey('brain.Patient', on_delete=models.CASCADE, related_name='jobs', null=True, blank=True)
	laparoscopy_patient = models.ForeignKey('laparoscopy.Patient', on_delete=models.CASCADE, related_name='jobs', null=True, blank=True)
	voice_caption = models.ForeignKey('maxillo.VoiceCaption', on_delete=models.CASCADE, related_name='jobs', null=True, blank=True)
	brain_voice_caption = models.ForeignKey('brain.VoiceCaption', on_delete=models.CASCADE, related_name='jobs', null=True, blank=True)
	laparoscopy_voice_caption = models.ForeignKey('laparoscopy.VoiceCaption', on_delete=models.CASCADE, related_name='jobs', null=True, blank=True)

	# IO
	input_files = models.JSONField(default=dict, blank=True, help_text='Dict of input object keys used by workers')
	output_files = models.JSONField(default=dict, blank=True, help_text='Dict of output object keys and metadata written on completion')

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
	# SLURM job id stamped by the runner worker (common.runner) for observability;
	# cleared on retry. The web app never reads it — dispatch is pure Celery.
	slurm_job_id = models.CharField(max_length=32, blank=True, default='', help_text='SLURM job id set by the runner worker')

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
		related_bits = []
		if self.patient_id:
			related_bits.append(f"patient:{self.patient_id}")
		if self.brain_patient_id:
			related_bits.append(f"brain_patient:{self.brain_patient_id}")
		if self.voice_caption_id:
			related_bits.append(f"voice:{self.voice_caption_id}")
		if self.brain_voice_caption_id:
			related_bits.append(f"brain_voice:{self.brain_voice_caption_id}")
		related_str = f" [{' | '.join(related_bits)}]" if related_bits else ""
		return f"Job {self.id} - {self.modality_slug} - {self.status}{related_str}"

	def can_retry(self):
		return self.status in {'processing', 'failed'} and self.retry_count < self.max_retries

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
		self.error_logs = ''
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

	def _pull_dependency_outputs(self):
		"""Merge each completed dependency's output_files into this job's
		input_files, keyed by the dependency's routing slug, so a step consumes
		its prerequisites' outputs. Mutates input_files in place (the caller
		saves); returns True if anything was added."""
		merged = dict(self.input_files or {})
		changed = False
		for dep in self.dependencies.all():
			if dep.status == 'completed' and dep.output_files:
				merged[dep.modality_slug or f'job_{dep.id}'] = dep.output_files
				changed = True
		if changed:
			self.input_files = merged
		return changed

	def update_status_based_on_dependencies(self):
		if self.status == 'dependency' and self.check_dependencies():
			# Feed prerequisites' outputs in as this job's input before it runs.
			self._pull_dependency_outputs()
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

class ProcessingJob(DomainFKAccessorMixin, models.Model):
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
	laparoscopy_patient = models.ForeignKey('laparoscopy.Patient', on_delete=models.CASCADE, related_name='processing_jobs', null=True, blank=True)
	voice_caption = models.ForeignKey('maxillo.VoiceCaption', on_delete=models.CASCADE, related_name='processing_jobs', null=True, blank=True)
	brain_voice_caption = models.ForeignKey('brain.VoiceCaption', on_delete=models.CASCADE, related_name='processing_jobs', null=True, blank=True)
	laparoscopy_voice_caption = models.ForeignKey('laparoscopy.VoiceCaption', on_delete=models.CASCADE, related_name='processing_jobs', null=True, blank=True)

	# File paths
	input_files = models.JSONField(default=dict, blank=True, help_text='Dict of input object keys used by workers')
	output_files = models.JSONField(default=dict, blank=True, help_text='Dict of output object keys and metadata written on completion')

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
		related_bits = []
		if self.patient_id:
			related_bits.append(f"patient:{self.patient_id}")
		if self.brain_patient_id:
			related_bits.append(f"brain_patient:{self.brain_patient_id}")
		if self.voice_caption_id:
			related_bits.append(f"voice:{self.voice_caption_id}")
		if self.brain_voice_caption_id:
			related_bits.append(f"brain_voice:{self.brain_voice_caption_id}")
		related_str = f" [{' | '.join(related_bits)}]" if related_bits else ""
		return f"ProcessingJob {self.id} - {self.job_type} - {self.status}{related_str}"

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


class FileRegistry(DomainFKAccessorMixin, models.Model):
	FILE_TYPE_CHOICES = [
		('cbct_raw', 'CBCT Raw'),
		('cbct_processed', 'CBCT Processed'),
		('ios_raw_upper', 'IOS Raw Upper'),
		('ios_raw_lower', 'IOS Raw Lower'),
		('ios_processed_upper', 'IOS Processed Upper'),
		('ios_processed_lower', 'IOS Processed Lower'),
		('ios_landmarks', 'IOS Landmarks'),
		('ios_landmarks_prediction', 'IOS Landmark Prediction'),
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
		('braintumor_mri_seg_raw', 'Brain MRI Segmentation Raw'),
		('braintumor_mri_seg_processed', 'Brain MRI Segmentation Processed'),
		# Maxillo image modalities
		('intraoral_raw', 'Intraoral Photographs Raw'),
		('intraoral_processed', 'Intraoral Photographs Processed'),
		('intraoral-photo_processed', 'Intraoral Photo Processed'),
		('teleradiography_raw', 'Teleradiography Raw'),
		('teleradiography_processed', 'Teleradiography Processed'),
		('panoramic_raw', 'panoramic Raw'),
		('panoramic_processed', 'panoramic Processed'),
		# Generic video modality (used by laparoscopy and any future video domain)
		('video_raw', 'Video Raw'),
		('video_processed', 'Video Processed'),
		# Dense annotation artifacts. Sparse annotations are MySQL rows (decision #20);
		# a labelmap is not sparse, and the governing rule already says dense segmentation
		# is a file artifact in object storage. Addressed by an AnnotationPayload, never
		# read back as the annotation record itself.
		('annotation_mask', 'Annotation Mask'),
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
	laparoscopy_patient = models.ForeignKey('laparoscopy.Patient', on_delete=models.CASCADE, related_name='files', null=True, blank=True)
	voice_caption = models.ForeignKey('maxillo.VoiceCaption', on_delete=models.CASCADE, related_name='files', null=True, blank=True)
	brain_voice_caption = models.ForeignKey('brain.VoiceCaption', on_delete=models.CASCADE, related_name='files', null=True, blank=True)
	laparoscopy_voice_caption = models.ForeignKey('laparoscopy.VoiceCaption', on_delete=models.CASCADE, related_name='files', null=True, blank=True)
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


class SystemCheck(models.Model):
	"""Recorded outcome of a maintenance task or health check run.

	One row per run (e.g. nightly database backup); the status dashboard
	reads the latest row per name.
	"""
	STATUS_CHOICES = [
		('ok', 'OK'),
		('warn', 'Warning'),
		('fail', 'Failed'),
	]

	name = models.CharField(max_length=100, db_index=True)
	status = models.CharField(max_length=10, choices=STATUS_CHOICES)
	ran_at = models.DateTimeField(auto_now_add=True, db_index=True)
	duration_ms = models.IntegerField(null=True, blank=True)
	details = models.JSONField(default=dict, blank=True)

	class Meta:
		ordering = ['-ran_at']

	def __str__(self):
		return f"{self.name} [{self.status}] @ {self.ran_at:%Y-%m-%d %H:%M}"


class SiteMaintenance(models.Model):
	"""Global operator-controlled access mode and planned-maintenance notice."""
	MODE_NORMAL = "normal"
	MODE_READ_ONLY = "read_only"
	MODE_LOCKDOWN = "lockdown"
	ACCESS_MODE_CHOICES = [
		(MODE_NORMAL, "Normal"),
		(MODE_READ_ONLY, "Read-only"),
		(MODE_LOCKDOWN, "Full lockdown"),
	]

	id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
	access_mode = models.CharField(
		max_length=20, choices=ACCESS_MODE_CHOICES, default=MODE_NORMAL
	)
	planned_message_enabled = models.BooleanField(default=False)
	planned_message = models.TextField(max_length=1000, blank=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		constraints = [
			models.CheckConstraint(
				condition=models.Q(id=1),
				name="common_site_maintenance_singleton",
			),
		]
		verbose_name = "site maintenance"
		verbose_name_plural = "site maintenance"

	def __str__(self):
		return f"Site maintenance ({self.get_access_mode_display()})"

	@classmethod
	def get_solo(cls):
		return cls.objects.get(pk=1)


class UserPreference(models.Model):
    """Cross-app per-user UI preferences.

    Supersedes the brain-only ``brain.UserPreference``: the report-template
    language now lives here so all three domains share one endpoint/context.
    """
    LANGUAGE_CHOICES = [
        ('it', 'Italian'),
        ('en', 'English'),
        ('de', 'German'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='ygg_preference')
    report_language = models.CharField(max_length=5, choices=LANGUAGE_CHOICES, default='it')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Preferences for {self.user.username}"


class RecentlyViewed(models.Model):
    """Per-user recently-opened patients across domains.

    Patients live in per-app tables, so this stores ``(domain, patient_pk)``
    rather than a hard FK. Powers the landing "Continue where you left off".
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='recently_viewed')
    domain = models.CharField(max_length=20)
    patient_pk = models.IntegerField()
    patient_name = models.CharField(max_length=255, blank=True)
    project_label = models.CharField(max_length=120, blank=True)
    icon = models.CharField(max_length=40, blank=True)
    viewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-viewed_at']
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'domain', 'patient_pk'), name='common_recentlyviewed_uniq'
            ),
        ]
        indexes = [models.Index(fields=['user', '-viewed_at'], name='common_rv_user_viewed_idx')]

    def __str__(self):
        return f"{self.user.username} → {self.domain}#{self.patient_pk}"


class ActivityEvent(models.Model):
    """Cross-domain audit/activity feed (patient-view Activity tab).

    Emitted via ``common.activity.log_activity`` at action sites (upload,
    processing complete, classification edit, caption add/edit, export).
    """
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='activity_events'
    )
    domain = models.CharField(max_length=20)
    patient_pk = models.IntegerField(null=True, blank=True)
    patient_name = models.CharField(max_length=255, blank=True)
    verb = models.CharField(max_length=40)
    target = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['domain', 'patient_pk', '-created_at'], name='common_ae_dom_pat_idx')]

    def __str__(self):
        return f"{self.actor_id or '?'} {self.verb} {self.domain}#{self.patient_pk}"


class Notification(models.Model):
    """Per-user in-app notification (topbar bell with unread count)."""
    LEVEL_CHOICES = [
        ('info', 'Info'),
        ('success', 'Success'),
        ('warning', 'Warning'),
        ('danger', 'Danger'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default='info')
    message = models.CharField(max_length=500)
    url = models.CharField(max_length=500, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'is_read', '-created_at'], name='common_notif_user_read_idx')]

    def __str__(self):
        return f"[{self.level}] {self.message[:40]} → {self.user.username}"


# The DICOM catalog (Phase 8) is defined in ``common/dicom/models.py`` -- its own
# module, because it is a self-contained subsystem and this file is long enough. It
# carries an explicit ``app_label``, so Django files it under ``common`` either way;
# this import is what makes ``from common.models import DicomSeries`` read like every
# other model here, and what guarantees the models are registered when this module is.
from common.dicom.models import (  # noqa: E402,F401  (placement is deliberate)
	DicomInstance,
	DicomSeries,
	SealedSeriesError,
)
