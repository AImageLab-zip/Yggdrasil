import logging

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from common.domains import DOMAIN_CHOICES
from laparoscopy.export_processor import LaparoscopyExportProcessor
from common.export_processing import ExportProcessor


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run a single export job synchronously (used by subprocess launcher).'

    def add_arguments(self, parser):
        parser.add_argument('export_id', type=int)
        parser.add_argument('--domain', choices=[slug for slug, _ in DOMAIN_CHOICES])

    def handle(self, *args, **options):
        export_id = options['export_id']
        domain = options.get('domain')

        export = None
        if domain:
            export = apps.get_model(domain, 'Export').objects.filter(id=export_id).first()
        else:
            # No domain given: probe each domain's table and infer the domain.
            for slug, _label in DOMAIN_CHOICES:
                export = apps.get_model(slug, 'Export').objects.filter(id=export_id).first()
                if export:
                    domain = slug
                    break

        if not export:
            raise CommandError(f'Export {export_id} not found')

        if export.status == 'pending':
            export.mark_processing()

        logger.info('Running export %s for domain %s', export_id, domain)
        if domain == 'laparoscopy':
            processor = LaparoscopyExportProcessor(export)
        else:
            processor = ExportProcessor(export, domain=domain)
        processor.process_export()

        self.stdout.write(self.style.SUCCESS(f'Export {export_id} finished with status {export.status}'))
