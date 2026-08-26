"""Compute the intensity statistics the viewer deliberately did not send.

``annotations.adapters.cornerstone`` refuses a probe's Hounsfield reading and an ROI's
mean, because neither is derivable from the geometry -- they need the voxels, which an
adapter does not have. This is the pass that supplies them, and it is a management
command for the same reason ``annotations_materialize_landmarks`` is: reading the bytes
means network I/O against object storage, which is unrunnable in CI and unresumable
inside a request.

**Why the server and not the browser.** Cornerstone's `cachedStats` would have been
free. It is also stale between edits by design, and it is computed on top of
``modalityScaleNifti``, whose rescale gate is wrong for two of the four
``scl_slope``/``scl_inter`` shapes (finding F1) -- so half the CBCT corpus would have
been recorded 1024 HU out with nothing saying so. nibabel applies the rescale
unconditionally and correctly, which is exactly the property the number needs.

**Statistics go on the revision that holds the geometry**, not on a new one.
``add_measurement`` enforces that a measured shape belongs to the revision it is
attached to, and that is the right rule: these are facts about *that* geometry, being
materialised late. Nothing about the revision's authorship changes -- the origin stays
whatever it was, and ``ever_annotated`` is untouched, because a machine computing a
mean is not annotation work.

Idempotent: a geometry that already carries intensity statistics is skipped unless
``--refresh`` is given, so the command can be re-run after a partial failure without
doubling every ROI's measurements.
"""

import math

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

import numpy as np

from annotations import roi_stats, services
from annotations.constants import CoordinateSystem, MeasurementKind, MeasurementUnit
from annotations.models import AnnotationSet, MeasurementItem, SpatialAnnotation3DItem
from common.file_access import exists as artifact_exists
from common.object_storage import download_to_tempfile

#: The statistic kinds this command writes, and the measurement kind each maps to.
STATISTIC_MEASUREMENTS = (
    ("mean", MeasurementKind.MEAN),
    ("stddev", MeasurementKind.STDDEV),
    ("min", MeasurementKind.MIN),
    ("max", MeasurementKind.MAX),
)

#: Volumes larger than this are skipped with a warning rather than loaded.
#:
#: ``get_fdata`` materialises the whole array, so a mis-registered file -- a zip, a
#: multi-gigabyte 4D series -- would take the process down mid-sweep. A CBCT at
#: 512x512x400 in float32 is about 400 MB, so this leaves real headroom while still
#: refusing the pathological case.
MAX_VOLUME_BYTES = 4 * 1024 * 1024 * 1024


class Command(BaseCommand):
    help = "Compute intensity statistics for stored ROI geometry (decision #11)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--patient",
            action="append",
            type=int,
            dest="patients",
            help="Limit to these patient ids. Repeatable.",
        )
        parser.add_argument(
            "--domain",
            choices=("maxillo", "brain", "laparoscopy"),
            help="Limit to one domain.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after this many volumes. 0 means no limit.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be written without writing it.",
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help=(
                "Recompute geometry that already carries statistics. Off by default so "
                "a re-run after a partial failure does not double every ROI."
            ),
        )

    def handle(self, *args, **options):
        sets = AnnotationSet.objects.filter(kind=services.MEASUREMENTS_KIND)
        if options.get("domain"):
            sets = sets.filter(domain=options["domain"])
        if options.get("patients"):
            sets = self._filter_patients(sets, options["patients"])

        # Group by resource so a volume shared by twenty measurements is downloaded
        # once. Without this the command is O(annotations) network round trips against
        # object storage, which on a real corpus is hours rather than minutes.
        by_resource = {}
        for annotation_set in sets.order_by("id"):
            revision = annotation_set.revisions.order_by("-revision_number").first()
            if revision is None:
                continue
            for item in SpatialAnnotation3DItem.objects.filter(revision=revision).order_by("id"):
                resource = self._resource_for(item)
                if resource is None:
                    continue
                by_resource.setdefault(resource.pk, (resource, []))[1].append(item)

        limit = options.get("limit") or 0
        written = skipped = failed = 0
        volumes = 0

        for resource, items in by_resource.values():
            if limit and volumes >= limit:
                self.stdout.write(f"stopping at --limit {limit}")
                break
            volumes += 1

            pending = [item for item in items if options["refresh"] or not self._has_statistics(item)]
            if not pending:
                skipped += len(items)
                continue

            try:
                volume, affine = self._load_volume(resource)
            except Exception as exc:  # noqa: BLE001 -- one bad file must not stop the sweep
                failed += len(pending)
                self.stderr.write(
                    self.style.WARNING(f"resource {resource.pk}: {exc}; {len(pending)} ROIs skipped")
                )
                continue

            for item in pending:
                try:
                    count = self._write_statistics(item, volume, affine, dry_run=options["dry_run"])
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    self.stderr.write(self.style.WARNING(f"item {item.pk}: {exc}"))
                    continue
                if count:
                    written += 1
                else:
                    skipped += 1

            # A CBCT is hundreds of megabytes; holding two while the next downloads is
            # how a sweep over a folder runs the box out of memory.
            del volume

        verb = "would write" if options["dry_run"] else "wrote"
        self.stdout.write(
            self.style.SUCCESS(
                f"{volumes} volumes: {verb} statistics for {written} ROIs, "
                f"skipped {skipped}, failed {failed}"
            )
        )
        if failed:
            self.stdout.write(
                "Failures are per-ROI and the command is idempotent: fix the cause and re-run."
            )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _filter_patients(sets, patient_ids):
        from django.db.models import Q

        return sets.filter(
            Q(patient__patient_id__in=patient_ids)
            | Q(brain_patient__patient_id__in=patient_ids)
            | Q(laparoscopy_patient__patient_id__in=patient_ids)
        )

    @staticmethod
    def _resource_for(item):
        """The volume resource an item's target points at, if it is one."""
        target = item.target
        resource = getattr(target, "source_resource", None)
        if resource is None or resource.file_id is None:
            return None
        return resource

    @staticmethod
    def _has_statistics(item):
        kinds = [kind for _, kind in STATISTIC_MEASUREMENTS] + [MeasurementKind.COUNT]
        return MeasurementItem.objects.filter(spatial_3d_item=item, kind__in=kinds).exists()

    def _load_volume(self, resource):
        """Download and open one volume, returning its data and its RAS affine.

        ``get_fdata`` applies ``scl_slope``/``scl_inter`` **unconditionally**, which is
        the whole reason the statistics are computed here rather than in the browser --
        see the module docstring. The array therefore comes back already in modality
        units, and the caller passes an identity LUT to `roi_stats`.
        """
        import nibabel as nib

        path = self._resource_path(resource)
        if not path:
            raise CommandError("resource has no readable file path")
        if not artifact_exists(path):
            raise CommandError(f"{path} is not in object storage")

        suffix = ".nii.gz" if path.endswith(".nii.gz") else ".nii"
        with download_to_tempfile(path, suffix=suffix) as local_path:
            image = nib.load(local_path)
            expected_bytes = int(np.prod(image.shape)) * 4
            if expected_bytes > MAX_VOLUME_BYTES:
                raise CommandError(
                    f"{path} would need {expected_bytes // (1024 ** 2)} MB as float32; "
                    "refusing rather than exhausting memory mid-sweep"
                )
            # float32 rather than the float64 get_fdata defaults to: it halves the
            # footprint and is far finer than any modality's meaningful precision.
            return image.get_fdata(dtype=np.float32), np.asarray(image.affine, dtype=float)

    @staticmethod
    def _resource_path(resource):
        """The object-storage key for a resource, following a bundle key if it has one."""
        file_obj = resource.file
        if not resource.file_key:
            return file_obj.file_path
        files = (file_obj.metadata or {}).get("files") or {}
        entry = files.get(resource.file_key) or {}
        return entry.get("path") or file_obj.file_path

    @staticmethod
    def _points_in_lps(item):
        """The item's points as patient LPS millimetres, or ``None`` if they are not.

        ``roi_stats`` takes LPS and maps it through the affine's inverse. Handing it
        ``volume_voxel`` points -- which are already indices -- would put the ROI
        somewhere arbitrary and still return a number, and ``resource_local`` is a
        mesh's object space with no relation to this volume at all. Both are skipped
        with a reason rather than computed wrongly.

        ``patient_ras_mm`` is converted rather than refused: it is the same physical
        point, two sign flips away, and refusing it would strand every annotation
        written against a NIfTI world frame.
        """
        frame = item.coordinate_system
        if frame == CoordinateSystem.PATIENT_LPS_MM:
            return item.points
        if frame == CoordinateSystem.PATIENT_RAS_MM:
            return [[-point[0], -point[1], point[2]] for point in item.points]
        return None

    @transaction.atomic
    def _write_statistics(self, item, volume, affine, *, dry_run):
        points = self._points_in_lps(item)
        if points is None:
            return 0

        stats = roi_stats.statistics_for_geometry(
            volume,
            affine,
            geometry_type=item.geometry_type,
            points=points,
            attributes=item.attributes or {},
            # Identity: nibabel already applied the rescale. Passing the header's LUT
            # here would apply it twice.
            rescale_slope=1.0,
            rescale_intercept=0.0,
        )
        if stats is None:
            # An ROI that covers no voxels -- outside the volume, or a shape with no
            # interior such as a two-point length. Nothing to report, and reporting a
            # zero would be a measurement.
            return 0
        if dry_run:
            return len(STATISTIC_MEASUREMENTS) + 1

        if self._has_statistics(item):
            MeasurementItem.objects.filter(
                spatial_3d_item=item,
                kind__in=[kind for _, kind in STATISTIC_MEASUREMENTS] + [MeasurementKind.COUNT],
            ).delete()

        for key, kind in STATISTIC_MEASUREMENTS:
            value = stats[key]
            if not math.isfinite(value):
                continue
            services.add_measurement(
                item.revision,
                item.target,
                kind=kind,
                value=value,
                # Hounsfield is the honest unit only for calibrated CT. Everything else
                # -- CBCT greyscale, MRI signal -- has no absolute unit, so the number
                # is reported without claiming one.
                unit=MeasurementUnit.HU if self._is_hounsfield(item) else MeasurementUnit.NONE,
                is_calibrated=True,
                spatial_3d_item=item,
                sample_count=stats["count"],
                selector=item.selector,
                label=item.label,
                order=item.order,
                attributes={"derived_from": "roi_stats", "statistic": key},
            )

        services.add_measurement(
            item.revision,
            item.target,
            kind=MeasurementKind.COUNT,
            value=stats["count"],
            unit=MeasurementUnit.NONE,
            is_calibrated=True,
            spatial_3d_item=item,
            # No `sample_count` here, and the validator is right to refuse one: the
            # count *is* the sample size, so recording it twice invites the two
            # disagreeing. The four statistics above carry it because a mean over an
            # unstated number of voxels is not a reportable figure.
            selector=item.selector,
            label=item.label,
            order=item.order,
            attributes={"derived_from": "roi_stats", "statistic": "count"},
        )
        return len(STATISTIC_MEASUREMENTS) + 1

    @staticmethod
    def _is_hounsfield(item):
        """Whether this volume's values are calibrated Hounsfield units.

        Only real CT earns the label. CBCT greyscale is vendor-dependent and is *not*
        Hounsfield -- calling it HU would dress a relative number up as a physical
        measurement, the same mistake ``MeasurementItem``'s calibration constraint
        exists to prevent for lengths.
        """
        resource = getattr(getattr(item, "target", None), "source_resource", None)
        file_type = getattr(getattr(resource, "file", None), "file_type", "") or ""
        return file_type.startswith("ct_")
