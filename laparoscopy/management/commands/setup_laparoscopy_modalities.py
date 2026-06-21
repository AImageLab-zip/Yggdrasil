from django.core.management.base import BaseCommand
from common.models import Project, Modality


class Command(BaseCommand):
    help = 'Create Laparoscopy project and register video modality'

    def handle(self, *args, **options):
        project, project_created = Project.objects.get_or_create(
            slug='laparoscopy',
            defaults={
                'name': 'Laparoscopy',
                'description': 'Laparoscopic surgery video project',
                'icon': 'fas fa-video',
                'is_active': True,
            }
        )

        if project_created:
            self.stdout.write(self.style.SUCCESS(f'Created project: {project.name}'))
        else:
            self.stdout.write(self.style.WARNING(f'Project already exists: {project.name}'))

        modalities_data = [
            {
                'name': 'Video',
                'slug': 'video',
                'description': 'Surgical video recording (.mp4, .avi)',
                'icon': 'fas fa-video',
                'label': 'Video',
                'supported_extensions': ['.mp4', '.avi'],
                'requires_multiple_files': False,
                'is_active': True,
            },
        ]

        for modality_data in modalities_data:
            modality, created = Modality.objects.get_or_create(
                slug=modality_data['slug'],
                defaults=modality_data,
            )

            if created:
                self.stdout.write(self.style.SUCCESS(f'Created modality: {modality.name}'))
            else:
                self.stdout.write(self.style.WARNING(f'Modality already exists: {modality.name}'))
                for key, value in modality_data.items():
                    if key != 'slug':
                        setattr(modality, key, value)
                modality.save()
                self.stdout.write(self.style.SUCCESS(f'Updated modality: {modality.name}'))

            project.modalities.add(modality)
            self.stdout.write(self.style.SUCCESS(f'Linked {modality.name} to {project.name} project'))

        self.stdout.write(self.style.SUCCESS(f'\nSuccessfully configured Laparoscopy project with {len(modalities_data)} modality'))
