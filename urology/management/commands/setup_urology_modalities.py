from django.core.management.base import BaseCommand
from common.models import AnnotationMethod, Modality, Project


class Command(BaseCommand):
    help = "Create Urology project and register urology modalities (MRI, WSI, and Confocale)"

    def handle(self, *args, **options):
        project, project_created = Project.objects.get_or_create(
            name="Urology",
            defaults={
                "slug": "urology",
                "domain": "urology",
                "description": "Urological oncology project with MRI, WSI, and Confocale modalities",
                "icon": "fas fa-microscope",
                "is_active": True,
            },
        )

        if project_created:
            self.stdout.write(self.style.SUCCESS(f"Created project: {project.name}"))
        else:
            self.stdout.write(self.style.WARNING(f"Project already exists: {project.name}"))
            project.description = "Urological oncology project with MRI, WSI, and Confocale modalities"
            project.save()

        modalities_data = [
            {
                "name": "Urology MRI",
                "slug": "urology-mri",
                "domain": "urology",
                "description": "Urology Magnetic Resonance Imaging (.nii, .nii.gz)",
                "icon": "fas fa-magnet",
                "label": "MRI",
                "supported_extensions": [".nii", ".nii.gz"],
                "requires_multiple_files": False,
                "is_active": True,
            },
            {
                "name": "Urology WSI",
                "slug": "urology-wsi",
                "domain": "urology",
                "description": "Digital Pathology Whole Slide Images (.svs, .tiff, .tif)",
                "icon": "fas fa-microscope",
                "label": "WSI",
                "supported_extensions": [".svs", ".tiff", ".tif"],
                "requires_multiple_files": False,
                "is_active": True,
            },
            {
                "name": "Urology Confocale",
                "slug": "urology-confocal",
                "domain": "urology",
                "description": "Confocale Microscopy Images (.svs, .tiff, .tif)",
                "icon": "fas fa-eye",
                "label": "Confocale",
                "supported_extensions": [".svs", ".tiff", ".tif"],
                "requires_multiple_files": False,
                "is_active": True,
            },
        ]

        for modality_data in modalities_data:
            modality, created = Modality.objects.get_or_create(
                slug=modality_data["slug"],
                defaults=modality_data,
            )

            if created:
                self.stdout.write(self.style.SUCCESS(f"Created modality: {modality.name}"))
            else:
                self.stdout.write(self.style.WARNING(f"Modality already exists: {modality.name}"))
                for key, value in modality_data.items():
                    if key != "slug":
                        setattr(modality, key, value)
                modality.save()
                self.stdout.write(self.style.SUCCESS(f"Updated modality: {modality.name}"))

            project.modalities.add(modality)
            self.stdout.write(
                self.style.SUCCESS(f"Linked {modality.name} to {project.name} project")
            )

        voice_caption = AnnotationMethod.objects.filter(slug="voice_caption").first()
        if voice_caption:
            project.annotation_methods.add(voice_caption)
            self.stdout.write(
                self.style.SUCCESS(f"Linked {voice_caption.name} to {project.name} project")
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nSuccessfully configured Urology project with {len(modalities_data)} modalities"
            )
        )
