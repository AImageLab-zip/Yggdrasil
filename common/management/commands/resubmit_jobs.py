"""Bulk (re)submit processing jobs for a modality across existing patients.

Use case: a new/updated algorithm ships for a modality and you want to process
patients that already exist in the database. For each matching patient:

  * if no Job exists for (patient, modality) -> create a fresh ``pending`` Job
    from the patient's raw file for that modality (the common on-upload path);
  * if a Job already exists and ``--include-existing`` is given -> flip it back
    to ``pending`` so a worker picks it up again.

Creating/flipping a Job to ``pending`` triggers the normal enqueue signal
(``common.signals._job_post_save``), so this reuses the exact same routing the
app uses on upload — nothing runner-facing changes. Runners stay domain-agnostic.

Examples::

    python manage.py resubmit_jobs --domain brain --modality braintumor_mri_seg
    python manage.py resubmit_jobs --domain maxillo --modality cbct --include-existing
    python manage.py resubmit_jobs --domain maxillo --modality ios --dry-run
"""

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from common.domains import DOMAINS, fk_fields_for
from common.job_routing import is_runner_enabled_for_modality
from common.models import FileRegistry, Job


class Command(BaseCommand):
    help = "Bulk (re)submit processing jobs for a modality across existing patients."

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True, choices=sorted(DOMAINS))
        parser.add_argument("--modality", required=True, help="Modality slug")
        parser.add_argument(
            "--include-existing",
            action="store_true",
            help="Also re-pend patients that already have a job for this modality.",
        )
        parser.add_argument(
            "--folder-id",
            type=int,
            default=None,
            help="Limit to patients in this folder id (of the domain).",
        )
        parser.add_argument("--limit", type=int, default=None, help="Cap patients processed.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would happen without writing.",
        )

    def handle(self, *args, **opts):
        domain = opts["domain"]
        slug = opts["modality"]
        include_existing = opts["include_existing"]
        dry = opts["dry_run"]

        if not is_runner_enabled_for_modality(slug):
            self.stdout.write(self.style.WARNING(
                f"Modality '{slug}' is disabled for runners; jobs would not be "
                f"enqueued. Enable its ProcessingStep first or expect no-ops."
            ))

        Patient = apps.get_model(domain, "Patient")
        patient_fk = fk_fields_for(domain)[0]
        uses_m2m = any(f.name == "folders" for f in Patient._meta.get_fields())

        patients = Patient.objects.all()
        if opts["folder_id"] is not None:
            if uses_m2m:
                patients = patients.filter(folders__id=opts["folder_id"]).distinct()
            else:
                patients = patients.filter(folder_id=opts["folder_id"])
        patients = patients.order_by("pk")
        if opts["limit"]:
            patients = patients[: opts["limit"]]

        created = repended = skipped_no_input = skipped_existing = 0

        for patient in patients:
            job = (
                Job.objects.filter(modality_slug=slug, domain=domain, **{patient_fk: patient})
                .order_by("-created_at")
                .first()
            )
            if job is not None:
                if not include_existing:
                    skipped_existing += 1
                    continue
                repended += 1
                if not dry:
                    job.status = "pending"
                    job.save()
                continue

            raw = (
                FileRegistry.objects.filter(
                    modality__slug=slug, domain=domain, **{patient_fk: patient}
                )
                .exclude(file_type__icontains="processed")
                .order_by("id")
                .first()
            )
            if raw is None or not raw.file_path:
                skipped_no_input += 1
                continue

            created += 1
            if not dry:
                Job.objects.create(
                    modality_slug=slug,
                    status="pending",
                    input_files={"input": raw.file_path},
                    domain=domain,
                    **{patient_fk: patient},
                )

        prefix = "[dry-run] " if dry else ""
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}domain={domain} modality={slug}: "
            f"created={created}, re-pended={repended}, "
            f"skipped_no_input={skipped_no_input}, skipped_existing={skipped_existing}"
        ))
