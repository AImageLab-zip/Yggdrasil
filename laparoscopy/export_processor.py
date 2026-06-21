"""Laparoscopy-specific export processor for subsampled video and NPZ masks."""

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
from PIL import Image, ImageDraw
from django.utils import timezone

from common.models import FileRegistry, Project
from common.object_storage import download_to_tempfile, get_object_storage
from .models import Export, Folder, Patient, RegionAnnotation, RegionType


logger = logging.getLogger(__name__)


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
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_packets",
            "-show_entries",
            "stream=width,height,avg_frame_rate,nb_frames,nb_read_packets,duration",
            "-of",
            "json",
            local_video_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "ffprobe is required in the web container to export laparoscopy videos."
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"ffprobe failed for {local_video_path}: {(result.stderr or result.stdout).strip()}"
            )

        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") or []
        if not streams:
            raise RuntimeError(f"No video stream found in {local_video_path}")
        stream = streams[0]

        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        if width <= 0 or height <= 0:
            raise RuntimeError(f"Invalid video dimensions in {local_video_path}")

        fps_raw = str(stream.get("avg_frame_rate") or "")
        fps = 0.0
        if "/" in fps_raw:
            num_raw, den_raw = fps_raw.split("/", 1)
            try:
                num = float(num_raw)
                den = float(den_raw)
                if den:
                    fps = num / den
            except (TypeError, ValueError, ZeroDivisionError):
                fps = 0.0
        elif fps_raw:
            try:
                fps = float(fps_raw)
            except (TypeError, ValueError):
                fps = 0.0
        if not math.isfinite(fps) or fps <= 0:
            fps = 1.0

        frame_count = 0
        for key in ("nb_frames", "nb_read_packets"):
            raw_value = stream.get(key)
            try:
                parsed = int(raw_value)
            except (TypeError, ValueError):
                parsed = 0
            if parsed > 0:
                frame_count = parsed
                break
        if frame_count <= 0:
            try:
                duration = float(stream.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration > 0:
                frame_count = max(1, int(math.ceil(duration * fps - 1e-9)))
        if frame_count <= 0:
            raise RuntimeError(f"Could not determine frame count for {local_video_path}")

        return {
            "width": width,
            "height": height,
            "fps": float(fps),
            "frame_count": int(frame_count),
        }

    @staticmethod
    def _frame_index_for_time(frame_time, fps, frame_count):
        try:
            frame_time = float(frame_time)
        except (TypeError, ValueError):
            frame_time = 0.0
        if not math.isfinite(frame_time) or frame_time < 0:
            frame_time = 0.0
        frame_index = int(round(frame_time * fps))
        return min(max(frame_index, 0), max(frame_count - 1, 0))

    @staticmethod
    def _clamp_coord(value, upper_bound):
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        if not math.isfinite(value):
            value = 0.0
        if upper_bound <= 0:
            return 0
        return min(max(int(round(value)), 0), upper_bound - 1)

    def _annotation_pairs(self, annotation, width, height):
        points = annotation.points if isinstance(annotation.points, list) else []
        if len(points) < 4 or len(points) % 2 != 0:
            return []
        pairs = []
        for idx in range(0, len(points), 2):
            x = self._clamp_coord(points[idx], width)
            y = self._clamp_coord(points[idx + 1], height)
            pairs.append((x, y))
        return pairs

    def _draw_polyline(self, draw, pairs, fill_value, stroke_width):
        if len(pairs) < 2:
            return
        draw.line(pairs, fill=fill_value, width=stroke_width)
        radius = max(1, stroke_width // 2)
        for x, y in pairs:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill_value)

    def _apply_annotation_to_layer(self, image, annotation, width, height):
        pairs = self._annotation_pairs(annotation, width, height)
        if not pairs:
            return

        draw = ImageDraw.Draw(image)
        tool = str(annotation.tool or "").strip().lower()
        stroke_width = max(1, int(round(float(annotation.stroke_width or 1.0))))

        if tool == "polygon":
            if len(pairs) >= 3:
                draw.polygon(pairs, fill=255, outline=255)
            return

        fill_value = 0 if tool == "eraser" else 255
        self._draw_polyline(draw, pairs, fill_value, stroke_width)

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
            frame_index = self._frame_index_for_time(annotation.frame_time, fps, frame_count)
            frame_map.setdefault(frame_index, []).append((class_index, annotation))
        return frame_map

    def _render_frame_masks(self, width, height, class_count, frame_annotations):
        if class_count <= 0:
            return np.zeros((0, height, width), dtype=np.uint8)

        layer_images = [Image.new("L", (width, height), 0) for _ in range(class_count)]
        for class_index, annotation in frame_annotations:
            if class_index < 0 or class_index >= class_count:
                continue
            self._apply_annotation_to_layer(
                layer_images[class_index], annotation, width=width, height=height
            )

        layers = []
        for image in layer_images:
            layer = (np.asarray(image, dtype=np.uint8) > 0).astype(np.uint8)
            layers.append(layer)
        return np.stack(layers, axis=0)

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

                            frame_progress_interval = max(1, video_meta["frame_count"] // 10)
                            for frame_index in range(video_meta["frame_count"]):
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
