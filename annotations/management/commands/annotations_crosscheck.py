"""Compare the legacy tables against ``annotations`` and exit non-zero on a gap.

This is what makes decision #6's cross-check release worth having. Keeping the
legacy tables readable for one release only helps if somebody actually compares
them, and "somebody looks at a dashboard" is not a gate. This is: it exits 0 when
every legacy annotation has a converted counterpart and non-zero otherwise, so
it can stand in front of the release that drops those tables (risk #19) and in
the prod-clone rehearsal.

It is read-only. It never writes, never repairs, and never converts -- a check
that fixes what it finds cannot be run to find out whether anything is wrong.

Three classes of finding, all reported:

* **missing** -- a legacy row with no converted counterpart. The conversion has
  not run, or it skipped this row.
* **orphan** -- a converted set for a patient with no legacy row behind it. Not
  necessarily wrong (new work is written straight to ``annotations``), so it is
  reported as informational unless ``--strict``.
* **fingerprint drift** -- a revision whose ``source_fingerprint`` no longer
  matches its resource's ``content_hash``. This one does not mean the conversion
  is wrong; it means the bytes an annotation was drawn on changed underneath it,
  which is the thing the raw-data lock and the F11 fix exist to prevent. Finding
  any is a signal worth stopping for.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from annotations.models import AnnotationRevision, AnnotationSet


class Command(BaseCommand):
    help = "Verify every legacy annotation has a converted counterpart."

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Also fail on converted sets with no legacy row behind them.",
        )
        parser.add_argument(
            "--show",
            type=int,
            default=20,
            help="How many examples to print per finding (0 = all).",
        )

    def handle(self, *args, **options):
        self.show = options["show"]
        self.strict = options["strict"]
        self.problems = 0
        self.informational = 0

        self._check_maxillo_classifications()
        self._check_intraoral()
        self._check_panoramic()
        self._check_ios_landmarks()
        self._check_laparoscopy()
        self._check_voice_captions()
        self._check_fingerprints()
        self._check_empty_sets()

        summary = f"{self.problems} problem(s), {self.informational} informational"
        if self.problems:
            self.stdout.write(self.style.ERROR(summary))
            raise CommandError("crosscheck failed")
        self.stdout.write(self.style.SUCCESS(summary))

    # ------------------------------------------------------------- reporting

    def _report(self, label, markers, *, fatal=True):
        markers = list(markers)
        if not markers:
            return
        if fatal:
            self.problems += len(markers)
            style = self.style.ERROR
        else:
            self.informational += len(markers)
            style = self.style.WARNING
        self.stdout.write(style(f"{label}: {len(markers)}"))
        shown = markers if self.show == 0 else markers[: self.show]
        for marker in shown:
            self.stdout.write(f"    {marker}")
        if len(shown) < len(markers):
            self.stdout.write(f"    ... and {len(markers) - len(shown)} more")

    def _converted_markers(self, kind):
        """Every ``note`` recorded by a conversion for one set kind.

        The note is the legacy row's identity -- ``legacy:<app>.<table>:<pk>`` --
        which is what lets this compare row for row rather than counting. A count
        comparison would pass while pointing at the wrong patients.
        """
        return set(
            AnnotationRevision.objects.filter(
                annotation_set__kind=kind, note__startswith="legacy:"
            ).values_list("note", flat=True)
        )

    def _compare(self, label, kind, expected_markers, *, fatal=True):
        converted = self._converted_markers(kind)
        expected = set(expected_markers)
        self._report(f"{label}: not converted", sorted(expected - converted), fatal=fatal)
        self._report(
            f"{label}: converted but the legacy row is gone",
            sorted(converted - expected),
            fatal=self.strict,
        )

    # --------------------------------------------------------------- surfaces

    def _check_maxillo_classifications(self):
        from maxillo.models import Classification

        self._compare(
            "maxillo classification",
            "occlusion_classification",
            (
                f"legacy:maxillo.classification:{pk}"
                for pk in Classification.objects.filter(
                    patient__isnull=False
                ).values_list("pk", flat=True)
            ),
        )

    def _check_intraoral(self):
        from maxillo.models import IntraoralToothSegmentation

        # An empty ``teeth`` map is a row that records the tool being opened,
        # not a segmentation, and the conversion skips it on purpose. Excluding
        # it here rather than tolerating it in the diff keeps a real gap visible.
        rows = IntraoralToothSegmentation.objects.exclude(teeth={}).exclude(
            teeth__isnull=True
        )
        self._compare(
            "intraoral segmentation",
            "intraoral_segmentation",
            (f"legacy:maxillo.intraoral:{pk}" for pk in rows.values_list("pk", flat=True)),
        )

    def _check_panoramic(self):
        from maxillo.models import PanoramicState

        self._compare(
            "panoramic arch",
            "panoramic_arch",
            (
                f"legacy:maxillo.panoramic:{pk}"
                for pk in PanoramicState.objects.values_list("pk", flat=True)
            ),
        )

    def _check_ios_landmarks(self):
        from common.models import FileRegistry

        rows = FileRegistry.objects.filter(
            file_type="ios_landmarks", domain="maxillo", patient__isnull=False
        )
        self._compare(
            "IOS landmarks",
            "ios_landmarks",
            (
                f"legacy:maxillo.ios_landmarks:{pk}"
                for pk in rows.values_list("pk", flat=True)
            ),
        )

    def _check_laparoscopy(self):
        from laparoscopy.models import Classification, QuadrantClassificationMarker

        self._compare(
            "laparoscopy notes",
            "study_notes",
            (
                f"legacy:laparoscopy.classification:{pk}"
                for pk in Classification.objects.filter(
                    patient__isnull=False
                ).values_list("pk", flat=True)
            ),
        )
        # **Video regions are deliberately not compared any more (Phase 10).**
        #
        # This check asked: does every legacy `RegionAnnotation` row have a converted
        # counterpart carrying the same points? That was the right question while both
        # sides were strokes. Decision #14 made the labelmap canonical, so the live
        # record for this surface is now a rasterised mask and the strokes that produced
        # it are not kept -- brush and eraser mutate pixels, and the revision chain is
        # the audit trail rather than a stroke log.
        #
        # Comparing a raster against a stroke would report a difference on every study,
        # and that difference *is* the design. Keeping the check with a loosened
        # comparison would be worse: it would still be green, and it would no longer be
        # evidence of anything.
        #
        # What replaces it as evidence is that `annotations_rasterize_video_masks` and
        # the export both rasterise through `laparoscopy.mask_raster`, so the migrated
        # mask and the mask the previous release exported are the same array by
        # construction. The legacy rows stay in place for decision #6's one release; the
        # notes and quadrant checks below are unaffected, because those are sparse rows
        # whose representation did not change.
        self._compare(
            "quadrant markers",
            "video_quadrants",
            (
                f"legacy:laparoscopy.quadrant:{pk}"
                for pk in QuadrantClassificationMarker.objects.values_list("pk", flat=True)
            ),
        )

    def _check_voice_captions(self):
        from brain.models import VoiceCaption as BrainVoiceCaption
        from laparoscopy.models import VoiceCaption as LaparoVoiceCaption
        from maxillo.models import VoiceCaption as MaxilloVoiceCaption

        expected = []
        for domain, model in (
            ("maxillo", MaxilloVoiceCaption),
            ("brain", BrainVoiceCaption),
            ("laparoscopy", LaparoVoiceCaption),
        ):
            expected.extend(
                f"legacy:{domain}.voice_caption:{pk}"
                for pk in model.objects.filter(patient__isnull=False).values_list(
                    "pk", flat=True
                )
            )
        self._compare("voice captions", "voice_caption", expected)

    # ----------------------------------------------------------- consistency

    def _check_fingerprints(self):
        """Has anything been rewritten underneath its annotations?

        A revision records what each target hashed to when it was written. A
        mismatch now means the bytes moved -- the exact failure the raw-data
        lock and the F11 affine gate exist to prevent. It does not prove the
        annotation is wrong, but it does mean nobody can say it is right.

        A resource with no recorded hash is skipped, not flagged: "we never
        knew" is not the same finding as "it changed", and conflating them would
        bury the real one.
        """
        drifted = []
        revisions = (
            AnnotationRevision.objects.exclude(source_fingerprint={})
            .select_related("annotation_set")
            .prefetch_related("annotation_set__targets__source_resource")
        )
        # ``chunk_size`` is required once ``prefetch_related`` is in play, and
        # it is what keeps this bounded on a production-sized table.
        for revision in revisions.iterator(chunk_size=500):
            current = {
                target.source_resource.identity_key: target.source_resource.content_hash
                for target in revision.annotation_set.targets.all()
            }
            for key, recorded in (revision.source_fingerprint or {}).items():
                if not recorded:
                    continue
                now = current.get(key)
                if now and now != recorded:
                    drifted.append(
                        f"set {revision.annotation_set_id} r{revision.revision_number} "
                        f"{key}: {recorded[:12]} -> {now[:12]}"
                    )
        self._report("source bytes changed after annotation", drifted)

    def _check_empty_sets(self):
        """A set with no revisions is what a crashed conversion leaves behind.

        Harmless on its own, but it makes the idempotence check in the
        conversion command treat the patient as unconverted forever, so it is
        worth surfacing rather than leaving to accumulate.
        """
        empty = (
            AnnotationSet.objects.annotate(n=Count("revisions"))
            .filter(n=0)
            .values_list("id", "domain", "kind")
        )
        self._report(
            "annotation sets with no revisions",
            [f"set {pk} ({domain}/{kind})" for pk, domain, kind in empty],
            fatal=False,
        )
