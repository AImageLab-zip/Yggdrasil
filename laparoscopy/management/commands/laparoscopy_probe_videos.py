"""Record the ffprobe result for every laparoscopy video that lacks one.

Phase 10 made the annotator refuse to mount for a video with no recorded probe --
correctly, because a browser cannot read a video's frame rate and guessing 30 for a
25 fps recording puts every mask on the wrong frame while looking entirely right. But
nothing recorded one. ``video_probe.probe_and_record`` had a single caller,
``annotations_rasterize_video_masks``, which visits **only patients that carry legacy
stroke rows** and writes onto the ``video_raw`` row, while the patient page prefers
the ``video_processed`` / ``compressed`` derivative. So a study with a video and no
legacy annotations could never mount the annotator, and the page said "No video
uploaded for this patient."

The upload path now records a probe as the file arrives
(``video_probe.probe_and_record_upload``). This command is for everything already in
the database, and for the derivatives the runner writes.

It probes **every** video row -- raw and processed alike -- because the page plays
whichever of them ranks highest, and a probe on a row nobody plays answers nothing.

It also reports when a patient's video rows **disagree on frame size**. That is worth
saying out loud: a mask rasterised against the raw file describes a different grid
from the compressed file the page plays, and no amount of probing reconciles them.
Reported, not repaired -- which of the two is right is a question about the study.

Idempotent and resumable, like its siblings: a row that already has a probe is
skipped unless ``--force``, and one bad file logs and does not stop the sweep.
"""

from pathlib import Path

from django.core.management.base import BaseCommand

from laparoscopy import video_probe

VIDEO_FILE_TYPES = ("video_raw", "video_processed")


class Command(BaseCommand):
    help = "Record the ffprobe result for laparoscopy videos that have none."

    def add_arguments(self, parser):
        parser.add_argument(
            "--patient", type=int, action="append", default=[],
            help="Restrict to these laparoscopy patient ids. Repeatable.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Stop after this many files (0 = no limit).",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Re-probe files that already carry a recorded probe.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be probed and write nothing.",
        )

    def handle(self, *args, **options):
        from common.models import FileRegistry

        rows = FileRegistry.objects.filter(
            domain="laparoscopy", file_type__in=VIDEO_FILE_TYPES
        ).order_by("laparoscopy_patient_id", "pk")
        if options["patient"]:
            rows = rows.filter(laparoscopy_patient_id__in=options["patient"])

        probed = skipped = failed = 0
        seen = []
        for row in rows:
            if not options["force"] and video_probe.recorded_probe(row) is not None:
                skipped += 1
                seen.append(row)
                continue
            if options["limit"] and probed >= options["limit"]:
                break

            label = f"file {row.id} (patient {row.laparoscopy_patient_id}, {row.file_type}"
            label += f"/{row.subtype})" if row.subtype else ")"

            if options["dry_run"]:
                self.stdout.write(f"would probe {label}")
                probed += 1
                continue

            try:
                probe = self._probe(row)
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the sweep
                failed += 1
                self.stderr.write(self.style.ERROR(f"{label}: {exc}"))
                continue

            probed += 1
            seen.append(row)
            self.stdout.write(
                self.style.SUCCESS(
                    f"probed {label}: {probe['width']}x{probe['height']}, "
                    f"{probe['fps']:.3f} fps, {probe['frame_count']} frames"
                )
            )

        self._report_frame_size_disagreements(seen)
        self.stdout.write(
            f"\n{probed} probed, {skipped} already recorded, {failed} failed."
        )

    def _probe(self, row):
        """Download the file once and cache the answer on its row."""
        from common.object_storage import download_to_tempfile

        suffix = Path(row.file_path or "").suffix or ".mp4"
        with download_to_tempfile(row.file_path, suffix=suffix) as local_path:
            return video_probe.probe_and_record(row, local_path)

    def _report_frame_size_disagreements(self, rows):
        """Name every patient whose video rows describe different frames.

        A stored mask is (height, width) on one grid. If the raw file and the
        compressed derivative are not the same size, a mask drawn against one cannot
        be drawn over the other, and the page plays whichever ranks highest. This is
        not something a probe can fix, so it is reported rather than papered over.
        """
        sizes_by_patient = {}
        for row in rows:
            probe = video_probe.recorded_probe(row)
            if probe is None:
                continue
            sizes_by_patient.setdefault(row.laparoscopy_patient_id, set()).add(
                (int(probe["width"]), int(probe["height"]))
            )

        for patient_id, sizes in sorted(sizes_by_patient.items()):
            if len(sizes) > 1:
                pretty = ", ".join(f"{w}x{h}" for w, h in sorted(sizes))
                self.stderr.write(
                    self.style.WARNING(
                        f"patient {patient_id}: video files disagree on frame size "
                        f"({pretty}). A mask drawn against one does not describe the "
                        f"other; decide which file the record belongs to."
                    )
                )
