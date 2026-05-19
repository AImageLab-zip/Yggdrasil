from common.models import Modality, Project
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Create/update Maxillo project modalities and link them to the Maxillo project"
    )

    def handle(self, *args, **options):
        maxillo_project, project_created = Project.objects.get_or_create(
            slug="maxillo",
            defaults={
                "name": "Maxillo",
                "description": "Dental and maxillofacial imaging project",
                "icon": "fas fa-tooth",
                "is_active": True,
            },
        )

        if project_created:
            self.stdout.write(
                self.style.SUCCESS(f"Created project: {maxillo_project.name}")
            )
        else:
            self.stdout.write(
                self.style.WARNING(f"Project already exists: {maxillo_project.name}")
            )

        modalities_data = [
            {
                "name": "CBCT",
                "slug": "cbct",
                "description": "Cone Beam Computed Tomography volume",
                "icon": "fas fa-cube",
                "label": "CBCT",
                "supported_extensions": [
                    ".dcm",
                    ".dicom",
                    ".nii",
                    ".nii.gz",
                    ".mha",
                    ".mhd",
                    ".nrrd",
                    ".nhdr",
                    ".zip",
                    ".tar",
                    ".tar.gz",
                    ".tgz",
                ],
                "requires_multiple_files": False,
                "is_active": True,
            },
            {
                "name": "IOS",
                "slug": "ios",
                "description": "Intraoral scans (upper and lower arches)",
                "icon": "fas fa-tooth",
                "label": "IOS",
                "supported_extensions": [".stl", ".obj", ".ply"],
                "requires_multiple_files": True,
                "is_active": True,
            },
            {
                "name": "Intraoral Photographs",
                "slug": "intraoral-photo",
                "description": "Multiple intraoral photographs (1-10 images)",
                "icon": "fas fa-camera",
                "label": "intraoral-photo",
                "supported_extensions": [".jpg", ".jpeg", ".png"],
                "requires_multiple_files": True,
                "is_active": True,
            },
            {
                "name": "Teleradiography",
                "slug": "teleradiography",
                "description": "Single teleradiography image",
                "icon": "fas fa-x-ray",
                "supported_extensions": [".jpg", ".jpeg", ".png"],
                "requires_multiple_files": False,
                "is_active": True,
            },
            {
                "name": "Panoramic",
                "slug": "panoramic",
                "description": "Panoramic image (uploaded orthopantomogram or generated from CBCT)",
                "icon": "fas fa-panorama",
                "label": "OPT",
                "supported_extensions": [".jpg", ".jpeg", ".png"],
                "requires_multiple_files": False,
                "is_active": True,
            },
            {
                "name": "RawZip",
                "slug": "rawzip",
                "description": "Archive with raw files attached to a patient",
                "icon": "fas fa-file-archive",
                "label": "RAW",
                "supported_extensions": [".zip", ".tar", ".tar.gz", ".tgz", ".7z"],
                "requires_multiple_files": False,
                "is_active": True,
            },
        ]

        for modality_data in modalities_data:
            modality, created = Modality.objects.get_or_create(
                slug=modality_data["slug"], defaults=modality_data
            )

            if created:
                self.stdout.write(
                    self.style.SUCCESS(f"Created modality: {modality.name}")
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f"Modality already exists: {modality.name}")
                )
                # Update existing modality with new data
                for key, value in modality_data.items():
                    if key != "slug":
                        setattr(modality, key, value)
                modality.save()
                self.stdout.write(
                    self.style.SUCCESS(f"Updated modality: {modality.name}")
                )

            maxillo_project.modalities.add(modality)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Linked {modality.name} to {maxillo_project.name} project"
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nSuccessfully configured Maxillo project with {len(modalities_data)} modalities"
            )
        )
