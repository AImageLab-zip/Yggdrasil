"""Delete the laparoscopy annotation mask archives left in object storage.

``annotations`` migration 0010 deleted the annotation rows; the labelmap archives they
pointed at are ``annotation_mask`` ``FileRegistry`` rows plus an object each, and
object-storage work is a management command, never a migration.

Idempotent and resumable: a row whose object is already gone is simply unregistered, and
one object that fails to delete is reported and left registered for the next run.
``--dry-run`` lists what would go.
"""

from django.core.management.base import BaseCommand, CommandError

from common.models import FileRegistry
from common.object_storage import ObjectStorageError, get_object_storage


class Command(BaseCommand):
    help = "Delete laparoscopy annotation_mask files (objects and registry rows)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        rows = FileRegistry.objects.filter(domain="laparoscopy", file_type="annotation_mask")
        storage = None if options["dry_run"] else get_object_storage()
        deleted = failed = 0
        for row in rows.order_by("pk").iterator():
            if storage is not None:
                try:
                    storage.delete(row.file_path)
                    row.delete()
                except ObjectStorageError as exc:
                    failed += 1
                    self.stderr.write(f"#{row.pk} {row.file_path}: {exc}")
                    continue
            deleted += 1
        verb = "would delete" if options["dry_run"] else "deleted"
        self.stdout.write(self.style.SUCCESS(f"{verb} {deleted}, failed {failed}"))
        if failed:
            raise CommandError(f"{failed} object(s) could not be deleted")
