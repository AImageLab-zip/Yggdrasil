import logging

from django.core.management.base import BaseCommand, CommandError

from ...models import Export as MaxilloExport
from ...utils.export_processor import ExportProcessor


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run a single export job synchronously (used by subprocess launcher).'

    def add_arguments(self, parser):
        parser.add_argument('export_id', type=int)

    def handle(self, *args, **options):
        export_id = options['export_id']

        export = MaxilloExport.objects.filter(id=export_id).first()
        if not export:
            raise CommandError(f'Export {export_id} not found')

        if export.status == 'pending':
            export.mark_processing()

        logger.info('Running export %s', export_id)
        processor = ExportProcessor(export, domain='maxillo')
        processor.process_export()

        self.stdout.write(self.style.SUCCESS(f'Export {export_id} finished with status {export.status}'))
