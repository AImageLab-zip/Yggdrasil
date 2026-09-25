"""Create the report templates this platform ships with.

The content comes from ``common/report_template_seed.py``, transcribed from the hardcoded
HTML the panel used to be. Run it once after upgrading; run it again whenever you like.

**It does not overwrite an admin's edits.** A template that already exists is left alone,
because the entire point of moving this into the database is that clinicians own the
wording -- a deploy that quietly reverted a corrected translation would make the feature
worse than the hardcoded version it replaces. ``--force`` is the explicit way to reset a
template to the shipped content, and it says what it replaced.

Not a data migration, though a pure-DB ``RunPython`` would be permitted here: a migration
runs exactly once, so a corrected translation would need a second migration, and
re-seeding a database where somebody deleted a template by accident would be impossible.
A command is re-runnable and diffable. For the same reason it is **not** called from
``entrypoint.sh``.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from common.models import ReportTemplate, ReportTemplateField
from common.report_template_seed import TEMPLATES


class Command(BaseCommand):
    help = "Create the shipped report templates (brain, maxillo, urology) if missing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Replace existing templates' fields with the shipped content.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change and write nothing.",
        )
        parser.add_argument(
            "--domain", default="",
            help="Only seed this domain.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        force = options["force"]
        dry_run = options["dry_run"]
        only = options["domain"].strip()

        created = updated = kept = 0
        for seed in TEMPLATES:
            if only and seed["domain"] != only:
                continue

            existing = ReportTemplate.objects.filter(
                domain=seed["domain"],
                modality_slug=seed["modality_slug"],
                project__isnull=True,
            ).first()

            scope = f"{seed['domain']}/{seed['modality_slug'] or 'all'}"

            if existing is not None and not force:
                kept += 1
                self.stdout.write(f"kept {scope} ({existing.fields.count()} fields)")
                continue

            if dry_run:
                verb = "would replace the fields of" if existing else "would create"
                self.stdout.write(f"{verb} {scope} ({len(seed['fields'])} fields)")
                continue

            template = existing or ReportTemplate(
                domain=seed["domain"], modality_slug=seed["modality_slug"]
            )
            template.name = seed["name"]
            template.icon = seed["icon"]
            template.subtitle_en = seed["subtitle_en"]
            template.subtitle_it = seed["subtitle_it"]
            template.subtitle_de = seed["subtitle_de"]
            template.is_active = True
            template.save()

            # Replace wholesale rather than merge: a field removed from the shipped
            # content must not survive a --force that is meant to restore it exactly.
            template.fields.all().delete()
            ReportTemplateField.objects.bulk_create([
                ReportTemplateField(template=template, order=order, **field)
                for order, field in enumerate(seed["fields"])
            ])

            if existing is not None:
                updated += 1
                self.stdout.write(self.style.WARNING(
                    f"reset {scope} to the shipped content ({len(seed['fields'])} fields)"
                ))
            else:
                created += 1
                self.stdout.write(self.style.SUCCESS(
                    f"created {scope} ({len(seed['fields'])} fields)"
                ))

        if dry_run:
            self.stdout.write("dry run: nothing was written")
            return
        self.stdout.write(
            f"{created} created, {updated} reset, {kept} left alone. "
            "Laparoscopy ships no template on purpose; its panel renders nothing until "
            "an admin writes one."
        )
