from django.core.management.base import BaseCommand
from common.models import Project, Modality


class Command(BaseCommand):
    help = 'Create Brain project and register brain MRI modalities'

    def handle(self, *args, **options):
        # Get or create Brain project
        brain_project, project_created = Project.objects.get_or_create(
            name='Brain',
            defaults={
                'slug': 'brain',
                'description': 'Brain tumor MRI project with T1, T2, FLAIR, and T1c modalities',
                'icon': 'fas fa-brain',
                'is_active': True,
            }
        )

        if project_created:
            self.stdout.write(
                self.style.SUCCESS(f'Created project: {brain_project.name}')
            )
        else:
            self.stdout.write(
                self.style.WARNING(f'Project already exists: {brain_project.name}')
            )

        # Define brain modalities
        modalities_data = [
            {
                'name': 'Brain MRI T1',
                'slug': 'braintumor-mri-t1',
                'description': 'Brain MRI T1 weighted sequence',
                'icon': 'fas fa-brain',
                'label': 'T1',
                'supported_extensions': ['.nii', '.nii.gz'],
                'requires_multiple_files': False,
                'is_active': True,
            },
            {
                'name': 'Brain MRI T2',
                'slug': 'braintumor-mri-t2',
                'description': 'Brain MRI T2 weighted sequence',
                'icon': 'fas fa-brain',
                'label': 'T2',
                'supported_extensions': ['.nii', '.nii.gz'],
                'requires_multiple_files': False,
                'is_active': True,
            },
            {
                'name': 'Brain MRI FLAIR',
                'slug': 'braintumor-mri-flair',
                'description': 'Brain MRI FLAIR sequence',
                'icon': 'fas fa-brain',
                'label': 'FLAIR',
                'supported_extensions': ['.nii', '.nii.gz'],
                'requires_multiple_files': False,
                'is_active': True,
            },
            {
                'name': 'Brain MRI T1c',
                'slug': 'braintumor-mri-t1c',
                'description': 'Brain MRI T1 contrast-enhanced sequence',
                'icon': 'fas fa-brain',
                'label': 'T1c',
                'supported_extensions': ['.nii', '.nii.gz'],
                'requires_multiple_files': False,
                'is_active': True,
            },
            {
                'name': 'Brain MRI Segmentation',
                'slug': 'braintumor-mri-seg',
                'description': 'Brain Tumor Segmentation Mask',
                'icon': 'fas fa-brain',
                'label': 'SEG',
                'supported_extensions': ['.nii', '.nii.gz'],
                'requires_multiple_files': False,
                'is_active': True,
            },
        ]

        # Create or update each modality and link to Brain project
        for modality_data in modalities_data:
            modality, created = Modality.objects.get_or_create(
                slug=modality_data['slug'],
                defaults=modality_data
            )

            if created:
                self.stdout.write(
                    self.style.SUCCESS(f'Created modality: {modality.name}')
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f'Modality already exists: {modality.name}')
                )
                # Update existing modality with new data
                for key, value in modality_data.items():
                    if key != 'slug':
                        setattr(modality, key, value)
                modality.save()
                self.stdout.write(
                    self.style.SUCCESS(f'Updated modality: {modality.name}')
                )

            # Link modality to Brain project
            brain_project.modalities.add(modality)
            self.stdout.write(
                self.style.SUCCESS(f'Linked {modality.name} to {brain_project.name} project')
            )

        self.stdout.write(
            self.style.SUCCESS(
                f'\nSuccessfully configured Brain project with {len(modalities_data)} modalities'
            )
        )
