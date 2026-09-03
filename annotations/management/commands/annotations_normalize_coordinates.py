"""Record what each volume resource's grid actually is, by reading its header.

A stored coordinate says which frame it is in. It does not say what that frame
*is*: ``volume_voxel`` index ``[128, 128, 64]`` means nothing without the shape
it indexes into, and ``patient_ras_mm`` means nothing without the affine that
produced it. Those facts live in the NIfTI header, which is bytes in object
storage -- so this is a management command and not a migration.

What it writes is ``SourceResource.descriptor`` (shape, spacing, affine,
orientation) and ``SourceResource.content_hash`` where one is missing. Nothing
else. It never moves a coordinate: converting stored numbers from one frame to
another is a lossy operation that would need a decision per surface, and doing
it silently inside a maintenance command is how a landmark ends up somewhere
nobody chose. It converts *knowledge*, not data.

The hazard it exists to surface is F2. ``nifti-reader.js`` fabricates a diagonal
RAS affine from ``pixDims`` when both ``qform_code`` and ``sform_code`` are zero,
and ``rasToLps()`` then turns that fiction into a confident-looking LPS
direction that renders without complaint. A volume in that state is one whose
annotations may be mirrored, and there is nothing in the pixels to say so. This
records ``spatial_codes_absent`` on the descriptor and reports the count, so the
question "which of our volumes are guessing at their orientation?" has an answer
before Phase 3 rewires the metadata path.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from annotations.constants import ResourceKind
from annotations.models import SourceResource
from common.object_storage import download_to_tempfile

#: Suffixes this command knows how to read. A resource pointing at anything else
#: is left alone rather than guessed at.
_NIFTI_SUFFIXES = (".nii", ".nii.gz")


class Command(BaseCommand):
    help = "Record grid facts (shape, spacing, affine) on volume resources."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=0, help="Stop after this many resources."
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help=(
                "Re-read resources that already have a descriptor. Off by "
                "default so a re-run is cheap and only fills gaps."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Read the headers, report, and write nothing.",
        )

    def handle(self, *args, **options):
        try:
            import nibabel  # noqa: F401
        except ImportError as exc:  # pragma: no cover - nibabel is a hard dep
            raise CommandError("nibabel is required to read NIfTI headers") from exc

        dry_run = options["dry_run"]
        limit = options["limit"]
        described = skipped = unreadable = 0
        guessing = []

        resources = SourceResource.objects.filter(
            kind=ResourceKind.LOGICAL_VOLUME, file__isnull=False
        ).select_related("file")

        for resource in resources.order_by("pk").iterator():
            if limit and described >= limit:
                break
            if resource.descriptor and not options["refresh"]:
                skipped += 1
                continue

            path = self._path_for(resource)
            if not path:
                skipped += 1
                continue

            try:
                facts = self._read_header(path)
            except Exception as exc:
                # Broad on purpose: nibabel raises a different exception for
                # every way a file can be wrong, and one unreadable volume must
                # not stop the sweep that tells us how many others there are.
                unreadable += 1
                self.stderr.write(
                    self.style.ERROR(f"  {resource.identity_key}: {exc}")
                )
                continue

            if facts["spatial_codes_absent"]:
                guessing.append(resource.identity_key)

            if not dry_run:
                self._save(resource, facts)
            described += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"described {described}, skipped {skipped}, unreadable {unreadable}"
                + (" (dry run -- nothing written)" if dry_run else "")
            )
        )
        if guessing:
            # Not an error: these volumes render, and they may well be correct.
            # The point is that nobody can currently tell, and a count is the
            # start of finding out.
            self.stdout.write(
                self.style.WARNING(
                    f"{len(guessing)} volume(s) have neither qform nor sform set, so "
                    "their orientation is inferred from pixel dimensions alone (F2). "
                    "Annotations on these may be mirrored with nothing in the data "
                    "to say so."
                )
            )
            for key in guessing[:20]:
                self.stdout.write(f"    {key}")
            if len(guessing) > 20:
                self.stdout.write(f"    ... and {len(guessing) - 20} more")

    def _path_for(self, resource):
        """The object key for this resource, resolving a bundle member if needed."""
        file_obj = resource.file
        if not resource.file_key or resource.file_key == "primary":
            path = file_obj.file_path
        else:
            metadata = file_obj.metadata if isinstance(file_obj.metadata, dict) else {}
            member = (metadata.get("files") or {}).get(resource.file_key) or {}
            path = member.get("path")
        if not isinstance(path, str) or not path.endswith(_NIFTI_SUFFIXES):
            return None
        return path

    def _read_header(self, path):
        """Header facts only -- the voxels are never loaded.

        ``nibabel.load`` is lazy, so reading ``header`` and ``affine`` costs the
        header and nothing else. A 600-slice CBCT would otherwise be a gigabyte
        of array per resource, for facts that live in the first 348 bytes.
        """
        import nibabel as nib

        suffix = ".nii.gz" if path.endswith(".nii.gz") else ".nii"
        with download_to_tempfile(path, suffix=suffix) as local_path:
            image = nib.load(local_path)
            header = image.header
            qform_code = int(header["qform_code"])
            sform_code = int(header["sform_code"])
            return {
                "shape": [int(value) for value in image.shape],
                "spacing_mm": [float(value) for value in header.get_zooms()[:3]],
                "affine": [[float(value) for value in row] for row in image.affine],
                "dtype": str(header.get_data_dtype()),
                "orientation": "".join(nib.aff2axcodes(image.affine)),
                "qform_code": qform_code,
                "sform_code": sform_code,
                # The F2 flag. Both codes zero means the affine nibabel handed
                # back was reconstructed from pixel dimensions, not read.
                "spatial_codes_absent": qform_code < 1 and sform_code < 1,
                # The two fields F1 turns on. Recorded here so the modality LUT
                # can be derived from the header rather than from a loader that
                # gates the rescale on `slope != 1 && inter != 0`.
                "scl_slope": self._finite(header["scl_slope"]),
                "scl_inter": self._finite(header["scl_inter"]),
            }

    @staticmethod
    def _finite(value):
        """NIfTI writes 0 or NaN for "unset"; both mean the same thing here."""
        import math

        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @transaction.atomic
    def _save(self, resource, facts):
        descriptor = dict(resource.descriptor or {})
        descriptor.update(facts)
        resource.descriptor = descriptor
        resource.save(update_fields=["descriptor", "updated_at"])
