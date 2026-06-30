from django.core.management.base import BaseCommand

from common.models import Project
from laparoscopy.models import RegionType


class Command(BaseCommand):
    help = 'Create standard Laparoscopy region annotation types'

    def handle(self, *args, **options):
        project, project_created = Project.objects.get_or_create(
            slug='laparoscopy',
            defaults={
                'name': 'Laparoscopy',
                'description': 'Laparoscopic surgery video project',
                'icon': 'fas fa-video',
                'is_active': True,
            },
        )

        if project_created:
            self.stdout.write(self.style.SUCCESS(f'Created project: {project.name}'))
        else:
            self.stdout.write(self.style.WARNING(f'Project already exists: {project.name}'))

        region_types_data = [
            {'name': 'lesione (o carinosi)', 'color': '#e6194b'},
            {'name': 'Fegato', 'color': '#3cb44b'},
            {'name': 'colecisti', 'color': '#ffe119'},
            {'name': 'diaframma', 'color': '#4363d8'},
            {'name': 'stomaco', 'color': '#f58231'},
            {'name': 'Colon', 'color': '#911eb4'},
            {'name': 'milza', 'color': '#46f0f0'},
            {'name': 'assi vascolari iliaci esterni', 'color': '#f032e6'},
            {'name': 'vescica', 'color': '#bcf60c'},
            {'name': 'utero', 'color': '#fabebe'},
            {'name': 'cicatrice ombelicale', 'color': '#008080'},
            {'name': 'legamento rotondo', 'color': '#e6beff'},
            {'name': 'uraco', 'color': '#9a6324'},
            {'name': 'omento', 'color': '#fffac8'},
            {'name': 'digiuno (treitz)', 'color': '#800000'},
            {'name': 'digiuno distale', 'color': '#aaffc3'},
            {'name': 'ileo prossimale', 'color': '#808000'},
            {'name': 'ileo distale (ultima ansa ileale - cieco)', 'color': '#ffd8b1'},
        ]

        for order, region_type_data in enumerate(region_types_data):
            region_type, created = RegionType.objects.get_or_create(
                project=project,
                name=region_type_data['name'],
                defaults={
                    'color': region_type_data['color'],
                    'order': order,
                },
            )

            changed_fields = []
            if region_type.color != region_type_data['color']:
                region_type.color = region_type_data['color']
                changed_fields.append('color')
            if region_type.order != order:
                region_type.order = order
                changed_fields.append('order')

            if created:
                self.stdout.write(self.style.SUCCESS(f'Created region type: {region_type.name}'))
            elif changed_fields:
                region_type.save(update_fields=changed_fields)
                self.stdout.write(self.style.SUCCESS(f'Updated region type: {region_type.name}'))
            else:
                self.stdout.write(self.style.WARNING(f'Region type already exists: {region_type.name}'))

        self.stdout.write(
            self.style.SUCCESS(
                f'\nSuccessfully configured {len(region_types_data)} Laparoscopy region types'
            )
        )
