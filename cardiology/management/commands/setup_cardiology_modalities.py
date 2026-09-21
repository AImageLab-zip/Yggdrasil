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
        else:
            for key, value in modality_data.items():
                if key != 'slug':
                    setattr(modality, key, value)
            modality.save()
            self.stdout.write(self.style.WARNING(f'Modality already exists: {modality.name}'))
        project.modalities.add(modality)

        # Categorical AF/NSR/Other/NI call is cardiology-specific; the text-note
        # annotation reuses the common "voice_caption" method every domain shares.
        # See common/migrations/0043_backfill_project_domain_and_roles.py, whose
        # one-time backfill this mirrors for a domain that did not exist yet.
        classification_method, _ = AnnotationMethod.objects.get_or_create(
            slug='ecg_classification',
            defaults={
                'name': 'ECG Classification',
                'description': 'AF / NSR / Other / NI call on an ECG recording.',
                'domain': 'cardiology',
                'icon': 'fas fa-heart-pulse',
            },
        )
        common_methods = AnnotationMethod.objects.filter(domain='')
        domain_methods = AnnotationMethod.objects.filter(domain='cardiology')
        project.annotation_methods.set(domain_methods | common_methods)
        self.stdout.write(self.style.SUCCESS(
            f'Enabled annotation methods: {", ".join(project.annotation_methods.values_list("slug", flat=True))}'
        ))

        self.stdout.write(self.style.SUCCESS('\nSuccessfully configured Cardiology project'))
