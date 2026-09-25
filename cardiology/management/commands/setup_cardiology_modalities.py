from django.core.management.base import BaseCommand
from common.models import AnnotationMethod, Project, Modality


class Command(BaseCommand):
    help = 'Create Cardiology project, register the ECG modality, and enable its annotation methods'

    def handle(self, *args, **options):
        project, project_created = Project.objects.get_or_create(
            slug='cardiology',
            defaults={
                'name': 'Cardiology',
                'description': 'ECG review and arrhythmia annotation',
                'icon': 'fas fa-heart-pulse',
                'domain': 'cardiology',
                'is_active': True,
            }
        )

        if project_created:
            self.stdout.write(self.style.SUCCESS(f'Created project: {project.name}'))
        else:
            self.stdout.write(self.style.WARNING(f'Project already exists: {project.name}'))

        modality_data = {
            'name': 'ECG',
            'slug': 'ecg',
            'domain': 'cardiology',
            'description': 'ECG recording (JSON)',
            'icon': 'fas fa-heart-pulse',
            'label': 'ECG',
            'supported_extensions': ['.json'],
            'requires_multiple_files': False,
            'is_active': True,
        }
        modality, created = Modality.objects.get_or_create(slug=modality_data['slug'], defaults=modality_data)
        if created:
            self.stdout.write(self.style.SUCCESS(f'Created modality: {modality.name}'))
            project.modalities.add(modality)
        else:
            # Seeds create what is missing and never overwrite: an existing row may
            # carry admin edits, and re-running a seed used to revert them silently.
            self.stdout.write(self.style.WARNING(f'Modality already exists: {modality.name}'))

        # The categorical AF/NSR/Other/NI call is cardiology's own method; the text
        # notes reuse the "voice_caption" method every domain shares. Added, never
        # `set()`, so a method an admin switched off elsewhere stays as they left it.
        classification_method, _ = AnnotationMethod.objects.get_or_create(
            slug='ecg_classification',
            defaults={
                'name': 'ECG Classification',
                'description': 'AF / NSR / Other / NI call on an ECG recording.',
                'domain': 'cardiology',
                'icon': 'fas fa-heart-pulse',
            },
        )
        voice_caption, _ = AnnotationMethod.objects.get_or_create(
            slug='voice_caption',
            defaults={'name': 'Voice Captions', 'is_active': True},
        )
        if project_created:
            project.annotation_methods.add(classification_method, voice_caption)
            self.stdout.write(self.style.SUCCESS(
                f'Enabled annotation methods: {classification_method.slug}, {voice_caption.slug}'
            ))

        self.stdout.write(self.style.SUCCESS('\nSuccessfully configured Cardiology project'))
