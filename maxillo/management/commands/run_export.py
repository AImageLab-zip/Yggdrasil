import logging

from django.core.management.base import BaseCommand, CommandError

from brain.models import Export as BrainExport
from laparoscopy.export_processor import LaparoscopyExportProcessor
from laparoscopy.models import Export as LaparoscopyExport

from ...models import Export as MaxilloExport
from ...utils.export_processor import ExportProcessor


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run a single export job synchronously (used by subprocess launcher).'

    def add_arguments(self, parser):
        parser.add_argument('export_id', type=int)
        parser.add_argument('--domain', choices=['maxillo', 'brain', 'laparoscopy'])

    def handle(self, *args, **options):
        export_id = options['export_id']
        domain = options.get('domain')

        export = None
        if domain == 'brain':
            export = BrainExport.objects.filter(id=export_id).first()
        elif domain == 'laparoscopy':
            export = LaparoscopyExport.objects.filter(id=export_id).first()
        elif domain == 'maxillo':
            export = MaxilloExport.objects.filter(id=export_id).first()
        else:
            export = MaxilloExport.objects.filter(id=export_id).first()
            if not export:
                export = BrainExport.objects.filter(id=export_id).first()
                if export:
                    domain = 'brain'
                else:
                    export = LaparoscopyExport.objects.filter(id=export_id).first()
                    if export:
                        domain = 'laparoscopy'
            else:
                domain = 'maxillo'

        if not export:
            raise CommandError(f'Export {export_id} not found')

        if not domain:
            if export.__class__.__module__.startswith('brain.'):
                domain = 'brain'
            elif export.__class__.__module__.startswith('laparoscopy.'):
                domain = 'laparoscopy'
            else:
                domain = 'maxillo'

        if export.status == 'pending':
            export.mark_processing()

        logger.info('Running export %s for domain %s', export_id, domain)
        if domain == 'laparoscopy':
            processor = LaparoscopyExportProcessor(export)
        else:
            processor = ExportProcessor(export, domain=domain)
        processor.process_export()

        self.stdout.write(self.style.SUCCESS(f'Export {export_id} finished with status {export.status}'))
