"""Convert the legacy laparoscopy stroke corpus into stored labelmaps.

Roadmap Phase 10, and the counterpart of ``annotations_convert_legacy``: that command
moves the *rows* into ``annotations/``, this one moves the *representation*. Decision
#14 makes the labelmap canonical, so the strokes a previous release recorded have to
become masks before the new viewer can read them -- otherwise a study opened after the
deploy would show nothing and look erased.

**It rasterises through the export's own code**, ``laparoscopy.mask_raster``, rather than
through a second implementation written to match it. That is what discharges risk 18
("decision #15 must preserve NPZ bytes") by construction: the mask this command stores
and the mask the old export drew are the same array because they came out of the same
function, replayed in the same ``created_at`` order -- which matters, because the eraser
is destructive and replaying out of order paints back what a later stroke removed.

What it does **not** do is delete anything. The legacy ``RegionAnnotation`` rows stay
exactly where they are: they are the cross-check's evidence for one release (decision
#6), and the export falls back to rasterising them for any patient this command has not
reached, so a deployment can run it at leisure instead of in the deploy window.

Idempotent and resumable, like every other command here: a patient whose latest revision
already carries a mask archive is skipped unless ``--force``.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from annotations.constants import AnnotationOrigin, PayloadFormat
from annotations.services.video import (
    REGIONS_KIND,
    encode_rle,
    region_label_schema,
    save_video_regions,
)
from laparoscopy import mask_raster


class Command(BaseCommand):
    help = "Rasterise legacy laparoscopy region strokes into stored labelmaps."

    def add_arguments(self, parser):
        parser.add_argument(
            "--patient", type=int, action="append", default=[],
            help="Restrict to these laparoscopy patient ids. Repeatable.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Stop after this many patients (0 = no limit).",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Re-rasterise patients that already carry a mask archive.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be written and write nothing.",
        )

    def handle(self, *args, **options):
        from laparoscopy.models import Patient, RegionAnnotation

        patients = Patient.objects.filter(
            region_annotations__isnull=False
        ).distinct().order_by("patient_id")
        if options["patient"]:
            patients = patients.filter(patient_id__in=options["patient"])
        if options["limit"]:
            patients = patients[: options["limit"]]

        written = skipped = failed = 0
        for patient in patients:
            try:
                result = self._rasterise(
                    patient, force=options["force"], dry_run=options["dry_run"]
                )
            except Exception as exc:  # noqa: BLE001 - one bad study must not stop the sweep
                failed += 1
                self.stderr.write(
                    self.style.ERROR(f"patient {patient.patient_id}: {exc}")
                )
                continue
            if result is None:
                skipped += 1
            else:
                written += 1
                self.stdout.write(
                    f"patient {patient.patient_id}: {result} annotated frames"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"{written} written, {skipped} skipped, {failed} failed"
                + (" (dry run)" if options["dry_run"] else "")
            )
        )
        if failed:
            raise CommandError(f"{failed} patient(s) could not be rasterised")

    # ------------------------------------------------------------------ one patient

    def _already_done(self, patient):
        from annotations.models import AnnotationSet

        annotation_set = AnnotationSet.objects.filter(
            domain="laparoscopy", laparoscopy_patient=patient, kind=REGIONS_KIND
        ).first()
        if annotation_set is None:
            return False
        revision = annotation_set.revisions.order_by("-revision_number").first()
        return revision is not None and revision.payloads.filter(
            format=PayloadFormat.NPZ_MASK
        ).exists()

    def _rasterise(self, patient, *, force, dry_run):
        from laparoscopy.models import RegionAnnotation

        if not force and self._already_done(patient):
            return None

        video = patient.files.filter(file_type="video_raw").order_by("pk").first()
        if video is None:
            raise CommandError("no video_raw file for the masks to be anchored to")

        # The frame size the strokes were drawn against. The legacy rows record a time
        # and a point list and never the dimensions, so it is read off the video with
        # the same probe the export uses -- guessing it would clamp every coordinate
        # into the wrong frame, silently and irreversibly.
        from pathlib import Path

        from common.object_storage import download_to_tempfile
        from laparoscopy import video_probe

        suffix = Path(video.file_path or "").suffix or ".mp4"
        with download_to_tempfile(video.file_path, suffix=suffix) as local_path:
            probe = video_probe.probe_video(local_path)
        width, height = int(probe["width"]), int(probe["height"])
        if width <= 0 or height <= 0:
            raise CommandError(f"ffprobe reported a {width}x{height} frame")

        rows = (
            RegionAnnotation.objects.filter(patient=patient)
            .select_related("region_type")
            .order_by("created_at", "id")
        )
        # Grouped by the millisecond the annotation model keys on, through the same
        # rounding the adapter uses, so a rasterised study and a converted one agree on
        # which frame a stroke belongs to.
        from annotations.adapters.legacy_laparoscopy import frame_time_to_ms

        by_time = {}
        for row in rows:
            if row.region_type is None:
                continue
            time_ms = frame_time_to_ms(row.frame_time)
            by_time.setdefault(time_ms, []).append(row)

        frames = {}
        for time_ms, strokes in by_time.items():
            codes = sorted({row.region_type.name for row in strokes})
            index_by_code = {code: index for index, code in enumerate(codes)}
            layers = mask_raster.render_layers(
                width,
                height,
                len(codes),
                [
                    (
                        index_by_code[row.region_type.name],
                        mask_raster.Stroke(
                            points=row.points,
                            tool=row.tool,
                            stroke_width=row.stroke_width,
                        ),
                    )
                    for row in strokes
                ],
            )
            frames[time_ms] = {
                code: layers[index_by_code[code]]
                for code in codes
                if layers[index_by_code[code]].any()
            }

        if dry_run:
            return len(frames)

        # Handed over in the wire format the live save takes, so this command exercises
        # the same decode path a browser does rather than a private one that could
        # drift from it.
        with transaction.atomic():
            save_video_regions(
                patient,
                video_file=video,
                width=width,
                height=height,
                frames=[
                    {
                        "timeMs": time_ms,
                        "regions": {
                            code: {"rle": encode_rle(mask)}
                            for code, mask in regions.items()
                        },
                    }
                    for time_ms, regions in sorted(frames.items())
                ],
                prompts=self._prompts(by_time),
                # Not human work: this is a format migration of annotations that were
                # already recorded, and recording it as manual would re-stamp the
                # authorship of every study it touches.
                origin=AnnotationOrigin.PREDICTION,
                label_schema=region_label_schema(patient.project),
            )
        return len(frames)

    @staticmethod
    def _prompts(by_time):
        """The SAM2 prompts carried on the legacy rows, in the live save's wire shape."""
        out = []
        for time_ms, rows in sorted(by_time.items()):
            for row in rows:
                for prompt in row.prompt_points or []:
                    if not isinstance(prompt, dict):
                        continue
                    out.append(
                        {
                            "timeMs": time_ms,
                            "regionCode": row.region_type.name if row.region_type else None,
                            "x": prompt.get("x", 0.0),
                            "y": prompt.get("y", 0.0),
                            "label": prompt.get("label", 1),
                        }
                    )
        return out
