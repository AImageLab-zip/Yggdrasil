from django.core.management.base import BaseCommand
from common.models import AnnotationMethod, Project, Modality, ProcessingStep


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
                'domain': 'laparoscopy',
            }
        )

        # The page's Annotation Mode button is gated on this method, so a fresh project
        # must have it (the migration only reaches projects that already existed).
        method, _ = AnnotationMethod.objects.get_or_create(
            slug='image_segmentation',
            defaults={
                'name': 'Image Segmentation',
                'description': 'Pixel-accurate label masks on still images and video frames.',
                'icon': 'fas fa-fill-drip',
                'domain': 'laparoscopy',
            },
        )
        if project_created:
            project.annotation_methods.add(method)

        if project_created:
            self.stdout.write(self.style.SUCCESS(f'Created project: {project.name}'))
        else:
            self.stdout.write(self.style.WARNING(f'Project already exists: {project.name}'))

        modalities_data = [
            {
                'name': 'Video',
                'slug': 'video',
                'domain': 'laparoscopy',
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
                # Seeds create what is missing and never overwrite: an existing row may
                # carry admin edits, and re-running a seed used to revert them silently.

            if created:
                project.modalities.add(modality)
                self.stdout.write(self.style.SUCCESS(f'Linked {modality.name} to {project.name} project'))

            # Without an enabled step an upload creates no Job and the page waits forever.
            # Non-blocking: the raw video must stay playable while the cluster works.
            step, step_created = ProcessingStep.objects.get_or_create(
                slug=modality.slug,
                defaults={
                    'modality': modality,
                    'name': 'Video compression and subsampling',
                    'algo_name': 'laparoscopy-video',
                    'is_blocking': False,
                    'is_enabled': True,
                },
            )
            if step_created:
                self.stdout.write(self.style.SUCCESS(f'Created processing step: {step.slug}'))

        self.stdout.write(self.style.SUCCESS(f'\nSuccessfully configured Laparoscopy project with {len(modalities_data)} modality'))
