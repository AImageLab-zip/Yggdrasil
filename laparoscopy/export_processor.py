"""Laparoscopy-specific export processor for subsampled video and NPZ masks.

Phase 10 changed where the masks come from, and deliberately did not change what they
look like. A patient whose annotations have been migrated into ``annotations/`` exports
the **stored labelmap**; a patient whose work is still in ``laparoscopy.RegionAnnotation``
exports strokes rasterised on the fly, exactly as before. Both go through
``laparoscopy.mask_raster``, so the two paths cannot drift, and a deployment that has not
yet run ``annotations_rasterize_video_masks`` still exports correctly rather than
exporting nothing -- which is what makes the migration safe to run at leisure instead of
in the deploy window.
"""

import io
import json
import logging
import math
import os
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from django.utils import timezone

from common.models import FileRegistry, Project
from common.object_storage import download_to_tempfile, get_object_storage
from . import mask_raster, video_probe
from .models import Export, Folder, Patient, RegionAnnotation, RegionType


logger = logging.getLogger(__name__)


def _as_stroke(annotation):
    """A ``RegionAnnotation`` row as the three fields the rasteriser reads."""
    return mask_raster.Stroke(
        points=annotation.points,
        tool=annotation.tool,
        stroke_width=annotation.stroke_width,
    )


@dataclass(frozen=True)
class LaparoscopyExportSource:
    patient: Patient
    video_file: FileRegistry


def _normalize_folder_ids(folder_ids):
    normalized = []
    for raw_value in folder_ids or []:
        try:
            folder_id = int(raw_value)
        except (TypeError, ValueError):
            continue
        if folder_id > 0:
            normalized.append(folder_id)
    return normalized


def _sanitize_archive_component(value):
    cleaned = "".join(
        c for c in str(value or "") if c.isalnum() or c in ("_", "-", " ")
    ).strip()
    return cleaned.replace(" ", "_") or "item"


def get_laparoscopy_export_folders():
    folders = list(Folder.objects.order_by("name", "id"))
    data = []
    for folder in folders:
        data.append(
            {
                "folder": folder,
                "full_path": folder.get_full_path(),
                "patient_count": int(folder.patients.count()),
            }
        )
    data.sort(key=lambda item: item["full_path"].lower())
    return data


def get_laparoscopy_region_types():
    project = Project.objects.filter(slug="laparoscopy").first()
    if not project:
        return []
    return list(RegionType.objects.filter(project=project).order_by("order", "name", "id"))


def list_laparoscopy_export_sources(folder_ids):
    normalized_folder_ids = _normalize_folder_ids(folder_ids)
    patients = list(Patient.objects.filter(folder_id__in=normalized_folder_ids).order_by("patient_id"))
    if not patients:
        return patients, []

    patient_ids = [patient.patient_id for patient in patients]
    latest_video_by_patient = {}
    subsampled_videos = (
        FileRegistry.objects.filter(
            domain="laparoscopy",
            file_type="video_processed",
            subtype="subsampled",
            laparoscopy_patient_id__in=patient_ids,
        )
        .select_related("laparoscopy_patient")
        .order_by("laparoscopy_patient_id", "-created_at", "-id")
    )
    for video_file in subsampled_videos:
        latest_video_by_patient.setdefault(video_file.laparoscopy_patient_id, video_file)

    sources = []
    for patient in patients:
        video_file = latest_video_by_patient.get(patient.patient_id)
        if video_file is not None:
            sources.append(LaparoscopyExportSource(patient=patient, video_file=video_file))

    return patients, sources


def build_laparoscopy_export_preview(folder_ids):
    patients, sources = list_laparoscopy_export_sources(folder_ids)
    total_size = sum(int(source.video_file.file_size or 0) for source in sources)
    return {
        "patient_count": len(patients),
        "exportable_patient_count": len(sources),
        "file_count": len(sources),
        "estimated_size_bytes": total_size,
    }


class LaparoscopyExportProcessor:
    """Generate laparoscopy exports with one NPZ mask stack per subsampled frame."""

    def __init__(self, export):
        self.export = export
        self.query_params = export.query_params or {}
        self.folder_ids = _normalize_folder_ids(self.query_params.get("folder_ids", []))

    def _update_progress(self, message, percent=None):
        update_kw = {"progress_message": message}
        if percent is not None:
            update_kw["progress_percent"] = min(100, max(0, int(percent)))
        Export.objects.filter(pk=self.export.pk).update(**update_kw)

    def _probe_video(self, local_video_path):
        """Delegated to ``laparoscopy.video_probe`` in Phase 10 -- see that module."""
        return video_probe.probe_video(local_video_path)

    # Rasterisation moved to laparoscopy/mask_raster.py in Phase 10: the migration
    # command that converts the legacy stroke corpus into stored labelmaps has to
    # produce the same pixels this export produces, and the only way to guarantee that
    # is for both to call the same function. These thin delegations stay so the
    # processor still reads as one object.

    @staticmethod
    def _frame_index_for_time(frame_time, fps, frame_count):
        return mask_raster.frame_index_for_time(frame_time, fps, frame_count)

    @staticmethod
    def _clamp_coord(value, upper_bound):
        return mask_raster.clamp_coord(value, upper_bound)

    def _annotation_pairs(self, annotation, width, height):
        return mask_raster.stroke_pairs(_as_stroke(annotation), width, height)

    def _apply_annotation_to_layer(self, image, annotation, width, height):
        mask_raster.apply_stroke_to_layer(image, _as_stroke(annotation), width, height)

    def _build_frame_annotation_map(self, patient, class_axis_by_region_type_id, fps, frame_count):
        annotations = (
            RegionAnnotation.objects.filter(
                patient=patient,
                region_type_id__in=class_axis_by_region_type_id.keys(),
            )
            .select_related("region_type")
            .order_by("created_at", "id")
        )
        frame_map = {}
        for annotation in annotations:
            class_index = class_axis_by_region_type_id.get(annotation.region_type_id)
            if class_index is None:
                continue
            frame_index = mask_raster.frame_index_for_time(
                annotation.frame_time, fps, frame_count
            )
            frame_map.setdefault(frame_index, []).append((class_index, annotation))
        return frame_map

    def _render_frame_masks(self, width, height, class_count, frame_annotations):
        return mask_raster.render_layers(
            width,
            height,
            class_count,
            [(index, _as_stroke(annotation)) for index, annotation in frame_annotations],
        )

    # --- the stored labelmap -------------------------------------------------------

    def _stored_masks(self, patient, class_axis, fps, frame_count, width, height):
        """``{frame_index: (class_count, h, w)}`` from ``annotations/``, or ``None``.

        ``None`` means this patient has not been migrated yet, and the caller falls back
        to rasterising the legacy strokes. That fallback is not a transitional
        convenience to be tidied away later: it is what lets
        ``annotations_rasterize_video_masks`` run at leisure instead of inside the deploy
        window, and what keeps an export correct for a study nobody has touched since.

        A stored mask whose frame size disagrees with the video is refused rather than
        resized. The masks were drawn against pixels; scaling them would move every
        boundary by an amount nobody chose, and the honest reading of the disagreement
        is that the video was re-encoded after the annotations were made.
        """
        from annotations.constants import PayloadFormat
        from annotations.models import AnnotationSet
        from annotations.services.video import REGIONS_KIND, read_mask_archive
        from common.file_access import open_binary

        annotation_set = AnnotationSet.objects.filter(
            domain="laparoscopy", laparoscopy_patient=patient, kind=REGIONS_KIND
        ).first()
        if annotation_set is None:
            return None
        revision = annotation_set.revisions.order_by("-revision_number").first()
        if revision is None:
            return None
        payload = revision.payloads.filter(
            format=PayloadFormat.NPZ_MASK, canonical_slot=1
        ).select_related("file").first()
        if payload is None or not payload.file_id:
            return None

        handle, _info = open_binary(payload.file.file_path)
        try:
            stored_width, stored_height, frames = read_mask_archive(handle.read())
        finally:
            close = getattr(handle, "close", None)
            if close is not None:
                close()
        if (stored_width, stored_height) != (width, height):
            raise RuntimeError(
                f"patient {patient.patient_id}: stored masks are "
                f"{stored_width}x{stored_height} but the video is {width}x{height}; "
                "the video was re-encoded after it was annotated"
            )

        axis_by_code = {entry["name"]: entry["axis"] for entry in class_axis}
        by_frame = {}
        for time_ms, regions in frames.items():
            frame_index = mask_raster.frame_index_for_ms(time_ms, fps, frame_count)
            layers = by_frame.setdefault(
                frame_index, np.zeros((len(class_axis), height, width), dtype=np.uint8)
            )
            for code, mask in regions.items():
                axis = axis_by_code.get(code)
                if axis is None:
                    # A region type deleted since the work was done. Dropping it is the
                    # only option -- the NPZ's class axis is the project's current
                    # vocabulary and there is no column to put it in -- but it is worth
                    # saying out loud rather than losing quietly.
                    logger.warning(
                        "patient %s: stored mask names region %r, which the project no "
                        "longer defines; omitted from the export",
                        patient.patient_id, code,
                    )
                    continue
                # Two annotated instants can round to one frame index at a low
                # sampling rate; OR rather than assign, so the later one does not
                # erase the earlier.
                layers[axis] |= (np.asarray(mask, dtype=np.uint8) > 0).astype(np.uint8)
        return by_frame

    def process_export(self):
        try:
            patients, sources = list_laparoscopy_export_sources(self.folder_ids)
            if not patients:
                self.export.mark_failed("No laparoscopy patients match the selected folders.")
                return

            if not sources:
                self.export.mark_failed(
                    "No selected laparoscopy patients have a subsampled video available for export."
                )
                return

            region_types = get_laparoscopy_region_types()
            if not region_types:
                self.export.mark_failed("No laparoscopy region types are configured for export.")
                return

            self.export.patient_count = len(sources)
            self.export.save(update_fields=["patient_count"])
            self._update_progress(f"Collected {len(sources)} exportable laparoscopy patients", 5)

            class_axis = []
            class_axis_by_region_type_id = {}
            for axis_index, region_type in enumerate(region_types):
                class_axis_by_region_type_id[region_type.id] = axis_index
                class_axis.append(
                    {
                        "axis": axis_index,
                        "region_type_id": region_type.id,
                        "name": region_type.name,
                        "color": region_type.color,
                        "order": region_type.order,
                    }
                )

            timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
            filename = f"export_{self.export.id}_{timestamp}.zip"
            storage_key = f"exports/{filename}"
            storage = get_object_storage()

            manifest = {
                "format_version": 1,
                "project": "laparoscopy",
                "export_id": self.export.id,
                "generated_at": timezone.now().isoformat(),
                "frame_sampling_fps": 1.0,
                "classes": class_axis,
                "patients": [],
                "query": {
                    "folder_ids": self.folder_ids,
                    "mask_format": "npz_multilayer",
                    "include_all_frames": True,
                    "video_subtype": "subsampled",
                },
            }

            with tempfile.TemporaryDirectory(prefix="tf_laparoscopy_export_") as tmpdir:
                export_path = os.path.join(tmpdir, filename)
                with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                    for patient_index, source in enumerate(sources, start=1):
                        patient = source.patient
                        progress_base = 10 + int(80 * (patient_index - 1) / max(len(sources), 1))
                        self._update_progress(
                            f"Exporting patient {patient_index}/{len(sources)} (ID {patient.patient_id})",
                            progress_base,
                        )

                        suffix = Path(source.video_file.file_path or "").suffix or ".mp4"
                        with download_to_tempfile(source.video_file.file_path, suffix=suffix) as local_video_path:
                            video_meta = self._probe_video(local_video_path)
                            frame_annotations = self._build_frame_annotation_map(
                                patient,
                                class_axis_by_region_type_id,
                                fps=video_meta["fps"],
                                frame_count=video_meta["frame_count"],
                            )

                            patient_folder = _sanitize_archive_component(
                                f"patient_{patient.patient_id}_{patient.name or ''}"
                            )
                            video_ext = Path(source.video_file.file_path or local_video_path).suffix or ".mp4"
                            video_zip_path = f"{patient_folder}/video/subsampled{video_ext}"
                            zipf.write(local_video_path, video_zip_path)

                            stored_masks = self._stored_masks(
                                patient,
                                class_axis,
                                fps=video_meta["fps"],
                                frame_count=video_meta["frame_count"],
                                width=video_meta["width"],
                                height=video_meta["height"],
                            )
                            empty_masks = np.zeros(
                                (len(class_axis), video_meta["height"], video_meta["width"]),
                                dtype=np.uint8,
                            )

                            frame_progress_interval = max(1, video_meta["frame_count"] // 10)
                            for frame_index in range(video_meta["frame_count"]):
                                if stored_masks is not None:
                                    # Decision #15: regenerated from labelmaps, not
                                    # replayed through PIL. The bytes are the same
                                    # because the labelmap was rasterised by the same
                                    # function this branch replaces.
                                    masks = stored_masks.get(frame_index, empty_masks)
                                else:
                                    masks = self._render_frame_masks(
                                        width=video_meta["width"],
                                        height=video_meta["height"],
                                        class_count=len(class_axis),
                                        frame_annotations=frame_annotations.get(frame_index, []),
                                    )
                                buffer = io.BytesIO()
                                np.savez_compressed(buffer, masks=masks)
                                zipf.writestr(
                                    f"{patient_folder}/masks/frame_{frame_index:06d}.npz",
                                    buffer.getvalue(),
                                )
                                if (frame_index + 1) % frame_progress_interval == 0:
                                    patient_pct = (frame_index + 1) / max(video_meta["frame_count"], 1)
                                    self._update_progress(
                                        (
                                            f"Writing masks for patient {patient.patient_id} "
                                            f"({frame_index + 1}/{video_meta['frame_count']} frames)"
                                        ),
                                        progress_base + int(80 / max(len(sources), 1) * patient_pct),
                                    )

                            manifest["patients"].append(
                                {
                                    "patient_id": patient.patient_id,
                                    "name": patient.name,
                                    "video_file_id": source.video_file.id,
                                    "video_file_key": source.video_file.file_path,
                                    "zip_video_path": video_zip_path,
                                    "frame_count": video_meta["frame_count"],
                                    "width": video_meta["width"],
                                    "height": video_meta["height"],
                                    "fps": video_meta["fps"],
                                }
                            )

                    zipf.writestr("manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))

                self._update_progress("Uploading export ZIP...", 95)
                storage.upload_file(
                    export_path,
                    key=storage_key,
                    content_type="application/zip",
                    metadata={
                        "export_id": str(self.export.id),
                        "user_id": str(getattr(self.export, "user_id", "") or ""),
                    },
                )
                actual_size = os.path.getsize(export_path)

            self.export.mark_completed(file_path=storage_key, file_size=actual_size)
            logger.info(
                "Laparoscopy export %s completed successfully. Size: %s bytes",
                self.export.id,
                actual_size,
            )
        except Exception as exc:
            logger.error(
                "Error processing laparoscopy export %s: %s",
                self.export.id,
                exc,
                exc_info=True,
            )
            self.export.mark_failed(str(exc))
