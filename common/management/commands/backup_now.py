from django.core.management.base import BaseCommand

from common.tasks import backup_database


class Command(BaseCommand):
    help = "Run a database backup synchronously (same code path as the nightly task)."

    def handle(self, *args, **options):
        result = backup_database()
        style = self.style.SUCCESS if result.get("status") == "ok" else self.style.ERROR
        self.stdout.write(style(str(result)))
        if result.get("status") != "ok":
            raise SystemExit(1)
