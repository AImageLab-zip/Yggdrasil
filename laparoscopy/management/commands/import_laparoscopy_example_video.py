from pathlib import Path

from django.contrib.auth.models import User
from django.core.files import File
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from common.models import Modality, Project
from laparoscopy.file_utils import save_video_to_dataset
from laparoscopy.models import Dataset, Folder, Patient


class Command(BaseCommand):
    help = 'Import an example laparoscopy video and enqueue its processing job'

    def add_arguments(self, parser):
        parser.add_argument('video_path', help='Local path to the example .mp4 or .avi video')
        parser.add_argument(
            '--name',
            default='Example Guided Tour',
            help='Patient name to create for the example video',
        )
        parser.add_argument(
            '--folder',
            default='Tutorial',
            help='Top-level folder name for the example patient',
        )
        parser.add_argument(
            '--dataset',
            default='Tutorial',
            help='Dataset name for the example patient; pass an empty string to skip',
        )
        parser.add_argument(
            '--visibility',
            choices=['public', 'private', 'debug'],
            default='debug',
            help='Visibility assigned to the example patient',
        )
        parser.add_argument(
            '--username',
            default='',
            help='Optional existing username to set as uploaded_by / dataset creator',
        )
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='Delete an existing patient with the same name before importing',
        )

    def handle(self, *args, **options):
        video_path = Path(options['video_path']).expanduser().resolve()
        if not video_path.is_file():
            raise CommandError(f'Video file does not exist: {video_path}')

        if video_path.suffix.lower() not in {'.mp4', '.avi'}:
            raise CommandError('Example video must be an .mp4 or .avi file')

        name = options['name'].strip()
        if not name:
            raise CommandError('--name cannot be empty')

        folder_name = options['folder'].strip()
        if not folder_name:
            raise CommandError('--folder cannot be empty')

        user = None
        username = options['username'].strip()
        if username:
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist as exc:
                raise CommandError(f'User does not exist: {username}') from exc

        call_command('setup_laparoscopy_modalities', verbosity=0)
        project = Project.objects.get(slug='laparoscopy')
        video_modality = Modality.objects.filter(slug='video').first()

        existing_patient = Patient.all_objects.filter(name=name).first()
        if existing_patient and not options['overwrite']:
            raise CommandError(
                f'Laparoscopy patient named "{name}" already exists. '
                'Use --overwrite to replace it.'
            )

        with transaction.atomic():
            if existing_patient:
                existing_patient.delete()
                self.stdout.write(self.style.WARNING(f'Deleted existing patient: {name}'))

            folder, _ = Folder.objects.get_or_create(name=folder_name, parent=None)

            dataset = None
            dataset_name = options['dataset'].strip()
            if dataset_name:
                dataset, _ = Dataset.objects.get_or_create(
                    name=dataset_name,
                    defaults={
                        'description': 'Example dataset for the laparoscopy guided tour',
                        'created_by': user,
                    },
                )

            patient = Patient.objects.create(
                name=name,
                folder=folder,
                dataset=dataset,
                visibility=options['visibility'],
                uploaded_by=user,
            )
            if video_modality:
                patient.modalities.add(video_modality)
                project.modalities.add(video_modality)

        try:
            with video_path.open('rb') as handle:
                django_file = File(handle, name=video_path.name)
                file_registry, job = save_video_to_dataset(patient, django_file)
        except Exception:
            patient.delete()
            raise

        if file_registry and job:
            file_registry.processing_job = job
            file_registry.save(update_fields=['processing_job'])

        self.stdout.write(self.style.SUCCESS(f'Imported example patient: {patient.name} ({patient.patient_id})'))
        if file_registry:
            self.stdout.write(self.style.SUCCESS(f'Created raw video file registry: {file_registry.id}'))
        else:
            self.stdout.write(self.style.WARNING('Raw video FileRegistry was not created'))

        if job:
            self.stdout.write(self.style.SUCCESS(f'Created pending video processing job: {job.id}'))
        else:
            self.stdout.write(self.style.WARNING('Video processing job was not created'))
