"""Convert the IOS landmark documents, which live in object storage.

Every other legacy annotation is a MySQL row. IOS landmarks are a JSON file per
patient, written by ``maxillo.views.patient_data`` to
``maxillo/processed/ios/ios_landmarks_patient_<id>.json`` and registered as a
``FileRegistry`` row of type ``ios_landmarks``. Reading them means network I/O
against Garage, which is why this is its own command and not part of
``annotations_convert_legacy``: a bucket that is unreachable, or a document that
one patient's file happens to be malformed, must not take the MySQL conversion
down with it.

``ios_landmarks_prediction`` rows are **not** converted. They are model output;
they have never locked a patient's raw data, and materializing them would create
annotation sets that look like human work in every listing. A prediction becomes
annotation work the moment somebody edits it -- at which point the save path
writes an ``ios_landmarks`` row, which this command does convert.
"""

import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from annotations import services
from annotations.adapters import legacy_maxillo
from annotations.constants import AnnotationOrigin
from annotations.models import AnnotationSet, LabelSchema
from common.models import AnnotationMethod, FileRegistry
from common.file_access import open_binary

#: Refuse to parse anything larger. A landmark document for one patient is a few
#: kilobytes; a hundred megabytes of it is a wrong file, and finding that out by
#: exhausting memory inside a loop over every patient is the worst way to learn.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


class Command(BaseCommand):
    help = "Convert IOS landmark documents from object storage into annotations."

    def add_arguments(self, parser):
        parser.add_argument(
            "--patient",
            action="append",
            type=int,
            help="Restrict to one maxillo patient id; repeatable.",
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Stop after this many documents."
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Read and convert, then roll everything back.",
        )
        parser.add_argument(
            "--continue-on-error",
            action="store_true",
            help=(
                "Report an unreadable or malformed document and carry on. Off "
                "by default, because a silent partial conversion of landmark "
                "data is indistinguishable from a complete one."
            ),
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.keep_going = options["continue_on_error"]
        converted = skipped = failed = 0

        method = AnnotationMethod.objects.filter(slug="ios_landmarks").first()
        schema = LabelSchema.objects.filter(slug="fdi-permanent", version=1).first()
        if schema is None:
            raise CommandError(
                "the fdi-permanent label schema is missing; run migrate first"
            )

        rows = FileRegistry.objects.filter(
            file_type="ios_landmarks", domain="maxillo", patient__isnull=False
        ).select_related("patient")
        if options["patient"]:
            rows = rows.filter(patient__patient_id__in=options["patient"])

        for row in rows.order_by("pk").iterator():
            if options["limit"] and converted >= options["limit"]:
                break
            marker = f"legacy:maxillo.ios_landmarks:{row.pk}"
            if self._already_converted(row.patient, marker):
                skipped += 1
                continue

            try:
                document = self._load(row)
                descriptors = legacy_maxillo.ios_landmarks(
                    document, patient_id=row.patient.patient_id
                )
            except (ValidationError, ValueError, OSError) as exc:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  {marker}: {exc}"))
                if not self.keep_going:
                    raise CommandError(
                        f"{marker} failed; re-run with --continue-on-error to skip it"
                    ) from exc
                continue

            if not descriptors:
                # An empty document means the tool was opened and nothing placed.
                skipped += 1
                continue

            self._write(row, marker, descriptors, method=method, schema=schema)
            converted += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"converted {converted}, skipped {skipped}, failed {failed}"
                + (" (dry run -- nothing written)" if self.dry_run else "")
            )
        )
        if failed:
            raise CommandError(f"{failed} document(s) failed to convert")

    def _already_converted(self, patient, marker):
        return AnnotationSet.objects.filter(
            kind="ios_landmarks", patient=patient, revisions__note=marker
        ).exists()

    def _load(self, row):
        """Read and parse one document, with a size ceiling.

        ``_normalize_loaded_landmarks`` in the app also unwraps worker
        aggregates; this does not, on purpose. A document shaped like a worker
        output in an ``ios_landmarks`` row is a row that should not exist, and
        quietly accepting it here would convert prediction output as if a person
        had placed it.
        """
        body, _ = open_binary(row.file_path)
        try:
            raw = body.read(MAX_DOCUMENT_BYTES + 1)
        finally:
            body.close()
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ValidationError(
                f"the document is over {MAX_DOCUMENT_BYTES} bytes; this is not a "
                "landmark file"
            )
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError(f"the document is not valid UTF-8 JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise ValidationError("the document must be a JSON object")
        return document

    @transaction.atomic
    def _write(self, row, marker, descriptors, *, method, schema):
        annotation_set = services.get_or_create_set(
            row.patient,
            "ios_landmarks",
            annotation_method=method,
            label_schema=schema,
            check_project=False,
        )
        # The mesh is the resource, and ``resource_local`` coordinates are only
        # meaningful against it -- but the legacy document does not say which
        # mesh, only which patient. The landmark file is the closest durable
        # anchor there is until Phase 6 rewires the tool to name the mesh, so it
        # is recorded as the target and the gap is stated rather than papered
        # over: ``role='landmark_document'``, not ``role='mesh'``.
        resource = services.register_file(row, content_hash=row.file_hash)
        has_primary = annotation_set.targets.filter(primary_slot=1).exists()
        target = services.attach_target(
            annotation_set,
            resource,
            role="landmark_document",
            primary=not has_primary,
        )
        revision = services.record_revision(
            annotation_set,
            author=None,
            origin=AnnotationOrigin.MIGRATION,
            note=marker,
        )
        services.apply_descriptors(revision, target, descriptors, require_labels=True)
        if self.dry_run:
            transaction.set_rollback(True)
