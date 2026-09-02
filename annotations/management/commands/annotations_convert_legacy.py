"""Convert the MySQL-resident legacy annotations into the ``annotations`` model.

A management command rather than a ``RunPython`` data migration, deliberately.
The row counts here are unbounded and unknown at authoring time; a migration
that converts them runs inside ``migrate``, cannot be resumed after a failure
halfway, and blocks a deploy while it works. This runs patient by patient in its
own transaction, is safe to re-run, and can be stopped and restarted. The
schema half stays in migrations where it belongs -- the additive-only rule and
the prod-clone rehearsal are about DDL, and this command emits none.

Idempotent by construction: a surface that already has an annotation set with at
least one revision is skipped. That makes "run it again after fixing one
patient" the normal operation rather than a special case, and it is what lets
the cross-check release keep both representations in step.

Bytes are never read here. IOS landmarks live in object storage and are the job
of ``annotations_materialize_landmarks``; this command handles only what MySQL
already holds.
"""

import traceback

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from annotations import services
from annotations.adapters import legacy_common, legacy_laparoscopy, legacy_maxillo
from annotations.constants import AnnotationOrigin, AnnotationStatus
from annotations.models import AnnotationSet, LabelSchema
from annotations.services.segmentation import IMAGE_ROLE
from common.domains import DOMAINS, fk_fields_for
from common.models import AnnotationMethod

#: ``AnnotationSet.kind`` -> the ``AnnotationMethod`` slug that gates it, where
#: one exists. The panoramic arch is absent on purpose: no registry entry gates
#: it, and its editability is governed by the annotation lock instead.
_METHOD_FOR_KIND = {
    "ios_landmarks": "ios_landmarks",
    "intraoral_segmentation": "intraoral_segmentation",
    "occlusion_classification": "classification",
    "video_regions": "video_regions",
    "voice_caption": "voice_caption",
}


class Command(BaseCommand):
    help = "Convert legacy per-domain annotation tables into the annotations app."

    def add_arguments(self, parser):
        parser.add_argument(
            "--domain",
            action="append",
            choices=sorted(DOMAINS),
            help="Restrict to one domain; repeatable. Default: all.",
        )
        parser.add_argument(
            "--surface",
            action="append",
            help=(
                "Restrict to one surface: classification, intraoral, panoramic, "
                "video_regions, quadrants, voice_captions. Repeatable."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after converting this many source rows (0 = no limit).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be converted and roll every write back.",
        )
        parser.add_argument(
            "--continue-on-error",
            action="store_true",
            help=(
                "Log a failing row and carry on instead of stopping. Off by "
                "default: an unexpected legacy shape is worth a human look, not "
                "a silently smaller conversion."
            ),
        )

    # ------------------------------------------------------------------ setup

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        self.keep_going = options["continue_on_error"]
        self.limit = options["limit"]
        self.domains = set(options["domain"] or DOMAINS)
        self.surfaces = set(options["surface"] or self._all_surfaces())
        unknown = self.surfaces - set(self._all_surfaces())
        if unknown:
            raise CommandError(f"unknown surface(s): {sorted(unknown)}")

        self.converted = 0
        self.skipped = 0
        self.failed = 0
        self._methods = {m.slug: m for m in AnnotationMethod.objects.all()}
        self._fdi_schema = LabelSchema.objects.filter(
            slug="fdi-permanent", version=1
        ).first()

        # ``classification`` and ``voice_captions`` exist in more than one
        # domain, so they are not gated on maxillo: each handler filters by
        # ``self.domains`` itself.
        self._run("classification", self._convert_classifications)
        if "maxillo" in self.domains:
            self._run("intraoral", self._convert_intraoral)
            self._run("panoramic", self._convert_panoramic)
        if "laparoscopy" in self.domains:
            self._run("video_regions", self._convert_video_regions)
            self._run("quadrants", self._convert_quadrant_markers)
        self._run("voice_captions", self._convert_voice_captions)

        self.stdout.write(
            self.style.SUCCESS(
                f"converted {self.converted}, skipped {self.skipped}, failed {self.failed}"
                + (" (dry run -- nothing written)" if self.dry_run else "")
            )
        )
        if self.failed:
            raise CommandError(f"{self.failed} row(s) failed to convert")

    @staticmethod
    def _all_surfaces():
        return (
            "classification",
            "intraoral",
            "panoramic",
            "video_regions",
            "quadrants",
            "voice_captions",
        )

    def _run(self, surface, handler):
        if surface not in self.surfaces:
            return
        self.stdout.write(f"-- {surface}")
        handler()

    def _budget_exhausted(self):
        return bool(self.limit) and self.converted >= self.limit

    # -------------------------------------------------------------- utilities

    def _method(self, kind):
        slug = _METHOD_FOR_KIND.get(kind)
        return self._methods.get(slug) if slug else None

    def _already_converted(self, patient, kind, *, marker=None):
        """Whether this surface is done for this patient.

        ``marker`` distinguishes several source rows that map to one set kind --
        one intraoral segmentation per image, one caption per recording -- by
        looking for a revision whose note names the source row. A set with no
        revisions is treated as not converted: an empty set is what a crashed
        run leaves behind.
        """
        patient_fk, _ = fk_fields_for(patient._meta.app_label)
        revisions = AnnotationSet.objects.filter(
            kind=kind, **{patient_fk: patient}
        ).values_list("revisions__note", flat=True)
        notes = {note for note in revisions if note}
        if marker is None:
            return bool(notes)
        return marker in notes

    @transaction.atomic
    def _write(self, *, patient, kind, marker, descriptors, resource=None, role="", origin=AnnotationOrigin.MIGRATION, label_schema=None, status=None, target_status=None):
        """One patient, one surface, one transaction.

        ``check_project=False``: this work predates the project registry, and a
        project that has since switched a method off must not lose annotations
        somebody actually made.

        ``status`` is the *set's*; ``target_status`` is this target's. The distinction
        is load-bearing for intraoral segmentation, where one legacy row is one
        photograph: the table carries ``is_confirmed`` per image, and folding N of them
        onto the set-level status let whichever row converted last decide the answer for
        all of them. Migration ``0003`` added the per-target column for exactly this.
        """
        annotation_set = services.get_or_create_set(
            patient,
            kind,
            annotation_method=self._method(kind),
            label_schema=label_schema,
            check_project=False,
        )
        target = None
        if resource is not None:
            # Claim the primary slot only if nothing holds it. A set can gather
            # several targets over a run -- one intraoral segmentation per
            # image -- and re-claiming would move the slot on every row, so the
            # "default frame" would end up being whichever image converted last.
            has_primary = annotation_set.targets.filter(primary_slot=1).exists()
            target = services.attach_target(
                annotation_set, resource, role=role, primary=not has_primary
            )
            if target_status is not None:
                services.set_target_status(target, target_status)
        revision = services.record_revision(
            annotation_set, author=None, origin=origin, note=marker, status=status
        )
        services.apply_descriptors(
            revision, target, descriptors, require_labels=label_schema is not None
        )
        if self.dry_run:
            transaction.set_rollback(True)
        return revision

    def _attempt(self, description, work):
        if self._budget_exhausted():
            return False
        try:
            work()
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            self.failed += 1
            self.stderr.write(self.style.ERROR(f"  {description}: {exc}"))
            if not self.keep_going:
                self.stderr.write(traceback.format_exc())
                raise CommandError(
                    f"{description} failed; re-run with --continue-on-error to skip it"
                ) from exc
            return False
        self.converted += 1
        return True

    # ------------------------------------------------------------- maxillo

    def _convert_classifications(self):
        """maxillo and laparoscopy share a table name and nothing else.

        ``maxillo.Classification`` holds five occlusion facets;
        ``laparoscopy.Classification`` holds a free-text ``notes`` field and no
        facets at all. They convert to different set kinds, because filing a
        surgeon's remark under an occlusion heading would make it unfindable and
        misdescribe it in every export.
        """
        from maxillo.models import Classification as MaxilloClassification

        plans = []
        if "maxillo" in self.domains:
            plans.append(("maxillo", MaxilloClassification, "occlusion_classification"))
        if "laparoscopy" in self.domains:
            from laparoscopy.models import Classification as LaparoClassification

            plans.append(("laparoscopy", LaparoClassification, "study_notes"))

        for domain, model, kind in plans:
            rows = model.objects.select_related("patient").order_by("pk").iterator()
            for row in rows:
                if self._budget_exhausted():
                    return
                if row.patient is None:
                    continue
                marker = f"legacy:{domain}.classification:{row.pk}"
                if self._already_converted(row.patient, kind, marker=marker):
                    self.skipped += 1
                    continue

                if kind == "study_notes":
                    descriptors = legacy_laparoscopy.classification_notes(
                        notes=getattr(row, "notes", ""), classifier=row.classifier
                    )
                else:
                    descriptors = legacy_maxillo.occlusion_classification(
                        {
                            facet: getattr(row, facet, None)
                            for facet in legacy_maxillo.OCCLUSION_FACETS
                        },
                        classifier=row.classifier,
                    )
                # A pipeline classification is machine output and must not lock
                # the case; a manual one is annotation work and must.
                origin = (
                    AnnotationOrigin.PREDICTION
                    if row.classifier == "pipeline"
                    else AnnotationOrigin.MIGRATION
                )
                self._attempt(
                    marker,
                    lambda row=row, kind=kind, marker=marker, descriptors=descriptors, origin=origin: self._write(
                        patient=row.patient,
                        kind=kind,
                        marker=marker,
                        descriptors=descriptors,
                        origin=origin,
                    ),
                )

    def _convert_intraoral(self):
        from maxillo.models import IntraoralToothSegmentation

        rows = (
            IntraoralToothSegmentation.objects.select_related("patient", "image_file")
            .order_by("pk")
            .iterator()
        )
        for row in rows:
            if self._budget_exhausted():
                return
            marker = f"legacy:maxillo.intraoral:{row.pk}"
            if self._already_converted(
                row.patient, "intraoral_segmentation", marker=marker
            ):
                self.skipped += 1
                continue
            descriptors = legacy_maxillo.intraoral_segmentation(row.teeth or {})
            if not descriptors:
                # An empty segmentation row is a record that somebody opened the
                # tool, not that they drew anything. Nothing to convert.
                self.skipped += 1
                continue

            def work(row=row, marker=marker, descriptors=descriptors):
                resource = services.register_file(row.image_file)
                self._write(
                    patient=row.patient,
                    kind="intraoral_segmentation",
                    marker=marker,
                    descriptors=descriptors,
                    resource=resource,
                    role=IMAGE_ROLE,
                    label_schema=self._fdi_schema,
                    # Per *target*: one legacy row is one photograph, and the set spans
                    # every photograph this patient owns.
                    target_status=(
                        AnnotationStatus.CONFIRMED
                        if row.is_confirmed
                        else AnnotationStatus.DRAFT
                    ),
                )

            self._attempt(marker, work)

    def _convert_panoramic(self):
        """The arch, its segmentation source **and** its baked strips.

        Through ``services.panoramic.save_panoramic_arch`` rather than this command's own
        ``_write``, which is the one place in this file that delegates wholesale. The
        reason is that a converted arch is not merely *similar* to a saved one -- the
        viewer decides whether to show a panoramic at all by asking
        ``current_browser_panoramic`` two questions, and the hand-rolled write answered
        neither:

        * **The strips were dropped.** ``PanoramicState`` carries ``mip_file`` and
          ``raysum_file``; ``panoramic_arch_state`` reads them back as the revision's
          ``png_render`` payloads. Converted without them, 54 studies had an arch, no
          strips, and ``patient_panoramic_data`` answered 404 over two PNGs sitting in
          object storage -- "Panoramic image not available", as reported.
        * **The segmentation source was dropped.** ``expected_fingerprint`` covers the
          volume *and* the segmentation, so a revision whose fingerprint names only the
          volume can never satisfy ``arch_describes_source`` and ``matchesSource`` is
          permanently false -- the same 404 by a second route.

        Both are things the live writer already does correctly, and a second
        implementation of "write a panoramic arch" is a second thing to get right on the
        one path where being wrong is silent. ``note=marker`` keeps
        ``_already_converted``'s bookkeeping, and ``save_panoramic_arch`` picks the origin
        from ``geometry_source`` itself -- ``auto`` is machine output and stays a
        prediction, which is the same rule this command applied by hand.
        """
        from annotations.services.panoramic import save_panoramic_arch
        from maxillo.models import PanoramicState

        rows = (
            PanoramicState.objects.select_related(
                "patient", "source_file", "source_segmentation_file", "mip_file", "raysum_file"
            )
            .order_by("pk")
            .iterator()
        )
        for row in rows:
            if self._budget_exhausted():
                return
            marker = f"legacy:maxillo.panoramic:{row.pk}"
            if self._panoramic_converted(row, marker):
                self.skipped += 1
                continue

            def work(row=row, marker=marker):
                if row.source_file is None:
                    raise ValidationError(
                        "the arch names no source volume, so its coordinates "
                        "cannot be anchored"
                    )
                strips = [
                    {
                        "variant": variant,
                        "file_obj": file_obj,
                        "content_hash": file_obj.file_hash or "",
                        "byte_size": file_obj.file_size,
                    }
                    for variant, file_obj in (("mip", row.mip_file), ("raysum", row.raysum_file))
                    if file_obj is not None
                ]
                save_panoramic_arch(
                    row.patient,
                    volume_file=row.source_file,
                    volume_file_key=row.source_file_key,
                    volume_hash=row.source_file_hash,
                    segmentation_file=row.source_segmentation_file,
                    segmentation_file_key=row.source_segmentation_key,
                    segmentation_hash=row.source_segmentation_hash,
                    spline=row.spline,
                    axial_slice=row.axial_slice,
                    volume_shape=row.volume_shape,
                    geometry_source=row.geometry_source,
                    default_mode=row.default_mode,
                    algorithm_version=row.algorithm_version,
                    strips=strips,
                    author=None,
                    note=marker,
                )
                if self.dry_run:
                    transaction.set_rollback(True)

            self._attempt(marker, transaction.atomic(work))

    def _panoramic_converted(self, row, marker):
        """Whether this arch is converted **and usable**, not merely present.

        The same rule ``_already_converted`` states for an empty set -- "a set with no
        revisions is what a crashed run leaves behind" -- one level down. An earlier
        version of this converter wrote the arch and dropped the strips, so 54 studies
        carried a `panoramic_arch` set that the viewer could do nothing with: it reads the
        strips back as the revision's ``png_render`` payloads and answers 404 without
        them. Treating that as done would make the defect permanent on every database
        that ran it, because a re-run is the only repair this command offers and the
        marker alone would skip forever.

        Nothing is deleted to repair it. ``AnnotationTarget`` is ``PROTECT``ed on purpose
        -- annotations are not disposable -- so the corrected conversion lands as a new
        revision on the same set, which is what revisions are for, and the previous one
        stays as the audit trail of what the earlier run produced.
        """
        if not self._already_converted(row.patient, "panoramic_arch", marker=marker):
            return False
        if row.mip_file is None and row.raysum_file is None:
            # Nothing to attach, so the arch alone is the whole record.
            return True
        patient_fk, _ = fk_fields_for(row.patient._meta.app_label)
        latest = (
            AnnotationSet.objects.filter(
                kind="panoramic_arch", **{patient_fk: row.patient}
            )
            .order_by("id")
            .first()
        )
        revision = latest.revisions.order_by("-revision_number").first() if latest else None
        return bool(revision and revision.payloads.exists())

    # --------------------------------------------------------- laparoscopy

    def _region_schema(self, project, extra_codes=()):
        """The per-project region vocabulary, widened to what this row actually says.

        Delegated to ``annotations.services.video`` in Phase 10: the live save needs the
        same schema, and two implementations of "which labels does this project have"
        would make a converted study and an edited one resolve their region names
        against different rows -- which the cross-check would then report as a gap on
        every field.

        ``extra_codes`` carries the row's own region name. Region types predate the
        project registry, so a stored stroke can reference a ``RegionType`` that now
        belongs to a *different* project than its patient does; without this the
        conversion aborts on it -- "label code 'Tool' is not defined in schema 3" -- and
        every later surface goes unconverted behind it.
        """
        from annotations.services.video import region_label_schema

        return region_label_schema(project, extra_codes=extra_codes)

    def _convert_video_regions(self):
        from laparoscopy.models import RegionAnnotation

        rows = (
            RegionAnnotation.objects.select_related(
                "patient", "patient__project", "region_type"
            )
            .order_by("pk")
            .iterator()
        )
        for row in rows:
            if self._budget_exhausted():
                return
            marker = f"legacy:laparoscopy.region:{row.pk}"
            if self._already_converted(row.patient, "video_regions", marker=marker):
                self.skipped += 1
                continue
            descriptors = legacy_laparoscopy.region_annotation(
                tool=row.tool,
                frame_time=row.frame_time,
                points=row.points or [],
                stroke_width=row.stroke_width,
                prompt_points=row.prompt_points or [],
                region_name=row.region_type.name if row.region_type else None,
            )

            def work(row=row, marker=marker, descriptors=descriptors):
                video = row.patient.files.filter(file_type="video_raw").order_by("pk").first()
                if video is None:
                    raise ValidationError(
                        "the patient has no video_raw file for the strokes to "
                        "be anchored to"
                    )
                resource = services.register_file(video, content_hash=video.file_hash)
                self._write(
                    patient=row.patient,
                    kind="video_regions",
                    marker=marker,
                    descriptors=descriptors,
                    resource=resource,
                    role="video",
                    label_schema=self._region_schema(
                        row.patient.project,
                        extra_codes=[row.region_type.name] if row.region_type else [],
                    ),
                )

            self._attempt(marker, work)

    def _convert_quadrant_markers(self):
        from laparoscopy.models import QuadrantClassificationMarker

        rows = (
            QuadrantClassificationMarker.objects.select_related(
                "patient", "quadrant_type"
            )
            .order_by("pk")
            .iterator()
        )
        for row in rows:
            if self._budget_exhausted():
                return
            marker = f"legacy:laparoscopy.quadrant:{row.pk}"
            if self._already_converted(row.patient, "video_quadrants", marker=marker):
                self.skipped += 1
                continue
            descriptors = legacy_laparoscopy.quadrant_marker(
                time_ms=row.time_ms,
                quadrant_name=row.quadrant_type.name if row.quadrant_type else None,
            )
            self._attempt(
                marker,
                lambda row=row, marker=marker, descriptors=descriptors: self._write(
                    patient=row.patient,
                    kind="video_quadrants",
                    marker=marker,
                    descriptors=descriptors,
                ),
            )

    # --------------------------------------------------------------- shared

    def _convert_voice_captions(self):
        from brain.models import VoiceCaption as BrainVoiceCaption
        from laparoscopy.models import VoiceCaption as LaparoVoiceCaption
        from maxillo.models import VoiceCaption as MaxilloVoiceCaption

        models = {
            "maxillo": MaxilloVoiceCaption,
            "brain": BrainVoiceCaption,
            "laparoscopy": LaparoVoiceCaption,
        }
        for domain, model in models.items():
            if domain not in self.domains:
                continue
            rows = model.objects.select_related("patient").order_by("pk").iterator()
            for row in rows:
                if self._budget_exhausted():
                    return
                if row.patient is None:
                    continue
                marker = f"legacy:{domain}.voice_caption:{row.pk}"
                if self._already_converted(row.patient, "voice_caption", marker=marker):
                    self.skipped += 1
                    continue
                descriptors = legacy_common.voice_caption(
                    transcript=getattr(row, "text_caption", "") or "",
                    duration=row.duration,
                    modality=getattr(row, "modality", "") or "",
                    status=getattr(row, "processing_status", "") or "",
                )
                self._attempt(
                    marker,
                    lambda row=row, marker=marker, descriptors=descriptors: self._write(
                        patient=row.patient,
                        kind="voice_caption",
                        marker=marker,
                        descriptors=descriptors,
                    ),
                )
