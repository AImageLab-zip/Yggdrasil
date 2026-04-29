"""Export processor for background export generation."""

import os
import sys
import zipfile
import logging
import subprocess
import tempfile
from pathlib import Path
from django.conf import settings
from django.utils import timezone

from common.file_access import exists as artifact_exists
from common.file_access import iter_bytes as iter_artifact_bytes
from common.object_storage import get_object_storage

logger = logging.getLogger(__name__)


class ExportProcessor:
    """Processes export jobs by querying patients, collecting files, and creating ZIP archives."""

    # Map modality slugs to raw/processed file types.
    MODALITY_TO_FILE_TYPES = {
        "cbct": {
            "raw": ["cbct_raw"],
            "processed": ["cbct_processed"],
        },
        "ios": {
            "raw": ["ios_raw_upper", "ios_raw_lower"],
            "processed": ["ios_processed_upper", "ios_processed_lower"],
        },
        "audio": {
            "raw": ["audio_raw"],
            "processed": ["audio_processed"],
        },
        "bite_classification": {
            "raw": [],
            "processed": ["bite_classification"],
        },
        "intraoral": {
            "raw": ["intraoral_raw"],
            "processed": ["intraoral_processed"],
        },
        "intraoral-photo": {
            "raw": ["intraoral_raw"],
            "processed": ["intraoral_processed"],
        },
        "teleradiography": {
            "raw": ["teleradiography_raw"],
            "processed": ["teleradiography_processed"],
        },
        "panoramic": {
            "raw": ["panoramic_raw"],
            "processed": ["panoramic_processed"],
        },
        "braintumor-mri-t1": {
            "raw": ["braintumor_mri_t1_raw"],
            "processed": ["braintumor_mri_t1_processed"],
        },
        "braintumor-mri-t1c": {
            "raw": ["braintumor_mri_t1c_raw"],
            "processed": ["braintumor_mri_t1c_processed"],
        },
        "braintumor-mri-t2": {
            "raw": ["braintumor_mri_t2_raw"],
            "processed": ["braintumor_mri_t2_processed"],
        },
        "braintumor-mri-flair": {
            "raw": ["braintumor_mri_flair_raw"],
            "processed": ["braintumor_mri_flair_processed"],
        },
        "rawzip": {
            "raw": ["generic_raw"],
            "processed": ["generic_processed"],
        },
    }

    def __init__(self, export, domain="maxillo"):
        """Initialize processor with export instance."""
        self.export = export
        self.domain = domain
        self.query_params = export.query_params
        self.folder_ids = self.query_params.get("folder_ids", [])
        self.modality_slugs = self.query_params.get("modality_slugs", [])
        self.filters = self.query_params.get("filters", {})
        self.has_content_selection = (
            "include_raw" in self.query_params
            or "include_processed" in self.query_params
        )
        if self.has_content_selection:
            self.include_raw = self._coerce_bool(
                self.query_params.get("include_raw"), default=False
            )
            self.include_processed = self._coerce_bool(
                self.query_params.get("include_processed"), default=False
            )
        else:
            # Legacy exports created before content selection existed.
            self.include_raw = True
            self.include_processed = True

    @staticmethod
    def _coerce_bool(value, default=False):
        """Convert common truthy/falsy values into bool."""
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _modality_file_type_groups(self, modality_slug):
        return self.MODALITY_TO_FILE_TYPES.get(
            modality_slug, {"raw": [], "processed": []}
        )

    def _modality_requested_file_types(self, modality_slug):
        groups = self._modality_file_type_groups(modality_slug)
        file_types = []
        if self.include_raw:
            file_types.extend(groups.get("raw", []))
        if self.include_processed:
            file_types.extend(groups.get("processed", []))
        return file_types

    def _file_type_matches_requested_content(self, file_type):
        is_raw = file_type.endswith("_raw") or file_type.startswith("ios_raw_")
        is_processed = (
            file_type.endswith("_processed")
            or file_type.startswith("ios_processed_")
            or file_type == "bite_classification"
        )

        if self.include_raw and is_raw:
            return True
        if self.include_processed and is_processed:
            return True
        return False

    def _patient_file_queryset(self, patients):
        from common.models import FileRegistry

        if self.domain == "brain":
            return FileRegistry.objects.filter(
                domain="brain", brain_patient__in=patients
            )
        return FileRegistry.objects.filter(domain="maxillo", patient__in=patients)

    def _build_no_files_found_error(self, patients):
        actual_modality_slugs = [s for s in self.modality_slugs if s != "reports"]
        selected_content = []
        if self.include_raw:
            selected_content.append("raw")
        if self.include_processed:
            selected_content.append("processed")

        if not selected_content:
            return "No export content selected. Enable at least one of Raw files or Processed files."

        patient_qs = self._patient_file_queryset(patients)
        modality_summaries = []
        for modality_slug in actual_modality_slugs:
            groups = self._modality_file_type_groups(modality_slug)
            parts = []
            if self.include_raw:
                raw_types = groups.get("raw", [])
                raw_count = (
                    patient_qs.filter(file_type__in=raw_types).count()
                    if raw_types
                    else 0
                )
                parts.append(f"raw={raw_count}")
            if self.include_processed:
                processed_types = groups.get("processed", [])
                processed_count = (
                    patient_qs.filter(file_type__in=processed_types).count()
                    if processed_types
                    else 0
                )
                parts.append(f"processed={processed_count}")
            if parts:
                modality_summaries.append(f"{modality_slug}({', '.join(parts)})")

        message = (
            f"No files found for {patients.count()} matching patient(s). "
            f"Requested content: {', '.join(selected_content)}."
        )
        if modality_summaries:
            message += f" Availability by modality: {'; '.join(modality_summaries)}."
        if self.include_processed and not self.include_raw:
            message += " Tip: enable Raw files if post-processing has not finished yet."
        return message

    def _update_progress(self, message, percent=None):
        """Update progress on the Export record for live feedback."""
        if self.domain == "brain":
            from brain.models import Export
        else:
            from ..models import Export
        update_kw = {"progress_message": message}
        if percent is not None:
            update_kw["progress_percent"] = min(100, max(0, int(percent)))
        Export.objects.filter(pk=self.export.pk).update(**update_kw)

    def query_patients(self):
        """Query patients based on folder_ids and filters. Apply AND logic for all filters."""
        if self.domain == "brain":
            from brain.models import Patient, VoiceCaption
        else:
            from ..models import Patient, VoiceCaption
        from common.models import FileRegistry, Modality

        # Start with folder filter
        patients = (
            Patient.objects.filter(folder_id__in=self.folder_ids)
            if self.folder_ids
            else Patient.objects.none()
        )

        if not patients.exists():
            return patients

        # Apply modality presence filters (checking for processed files)
        if self.filters.get("has_cbct"):
            cbct_file_types = self._modality_requested_file_types("cbct")
            if not cbct_file_types:
                return patients.none()
            file_filter = {"file_type__in": cbct_file_types, "domain": self.domain}
            if self.domain == "brain":
                cbct_patients = Patient.objects.filter(
                    patient_id__in=FileRegistry.objects.filter(
                        **file_filter
                    ).values_list("brain_patient_id", flat=True)
                ).distinct()
            else:
                cbct_patients = Patient.objects.filter(
                    files__file_type__in=cbct_file_types
                ).distinct()
            patients = patients.filter(
                patient_id__in=cbct_patients.values_list("patient_id", flat=True)
            )

        if self.filters.get("has_ios"):
            ios_file_types = self._modality_requested_file_types("ios")
            if not ios_file_types:
                return patients.none()
            if self.domain == "brain":
                ios_patients = Patient.objects.filter(
                    patient_id__in=FileRegistry.objects.filter(
                        domain="brain",
                        file_type__in=ios_file_types,
                    ).values_list("brain_patient_id", flat=True)
                ).distinct()
            else:
                ios_patients = Patient.objects.filter(
                    files__file_type__in=ios_file_types
                ).distinct()
            patients = patients.filter(
                patient_id__in=ios_patients.values_list("patient_id", flat=True)
            )

        # Dynamic modality presence filters
        for key, value in self.filters.items():
            if key.startswith("has_") and not key.startswith("has_reports_") and value:
                modality_slug = key.replace("has_", "")
                file_types = self._modality_requested_file_types(modality_slug)
                if file_types:
                    if self.domain == "brain":
                        modality_patients = Patient.objects.filter(
                            patient_id__in=FileRegistry.objects.filter(
                                domain="brain", file_type__in=file_types
                            ).values_list("brain_patient_id", flat=True)
                        ).distinct()
                    else:
                        modality_patients = Patient.objects.filter(
                            files__file_type__in=file_types
                        ).distinct()
                    patients = patients.filter(
                        patient_id__in=modality_patients.values_list(
                            "patient_id", flat=True
                        )
                    )

        # Report presence filters
        for key, value in self.filters.items():
            if key.startswith("has_reports_") and value:
                modality_slug = key.replace("has_reports_", "")
                try:
                    modality = Modality.objects.get(slug=modality_slug)
                    # Patients with voice captions for this modality
                    report_patients = (
                        Patient.objects.filter(
                            voice_captions__modality=modality,
                            voice_captions__text_caption__isnull=False,
                        )
                        .exclude(voice_captions__text_caption="")
                        .distinct()
                    )
                    patients = patients.filter(
                        patient_id__in=report_patients.values_list(
                            "patient_id", flat=True
                        )
                    )
                except Modality.DoesNotExist:
                    pass

        return patients.distinct()

    def collect_files(self, patients):
        """Collect files from FileRegistry for each patient and selected modalities."""
        if self.domain == "brain":
            from brain.models import VoiceCaption
        else:
            from ..models import VoiceCaption
        from common.models import FileRegistry, Modality

        files_to_export = []
        total_size = 0

        # Separate reports from actual modalities
        actual_modality_slugs = [
            slug for slug in self.modality_slugs if slug != "reports"
        ]

        # Get file types for selected modalities (excluding reports)
        file_types = []
        for modality_slug in actual_modality_slugs:
            file_types.extend(self._modality_requested_file_types(modality_slug))
        file_types = list(set(file_types))

        logger.info(
            f"Collecting files for modalities: {actual_modality_slugs}, file_types: {file_types}"
        )

        # Also check by modality relationship
        modality_objects = Modality.objects.filter(slug__in=actual_modality_slugs)
        logger.info(f"Found {modality_objects.count()} modality objects")

        for patient in patients:
            # Collect files from FileRegistry (only processed files)
            # Build query to match file_type in our list OR (modality match AND file is processed)
            from django.db.models import Q

            # Base query: match by file_type (this catches files even if modality is None)
            query = Q(file_type__in=file_types)

            # For modality-based matching, also include files that match by modality relationship
            # and selected content types (raw and/or processed)
            if modality_objects.exists():
                content_query = Q()
                if self.include_raw:
                    content_query |= Q(file_type__endswith="_raw") | Q(
                        file_type__startswith="ios_raw_"
                    )
                if self.include_processed:
                    content_query |= (
                        Q(file_type__endswith="_processed")
                        | Q(file_type__startswith="ios_processed_")
                        | Q(file_type="bite_classification")
                    )
                if content_query:
                    query |= Q(modality__in=modality_objects) & content_query

            if self.domain == "brain":
                patient_files = (
                    FileRegistry.objects.filter(domain="brain", brain_patient=patient)
                    .filter(query)
                    .distinct()
                )
            else:
                patient_files = (
                    FileRegistry.objects.filter(domain="maxillo", patient=patient)
                    .filter(query)
                    .distinct()
                )
            logger.info(
                f"Patient {patient.patient_id}: found {patient_files.count()} files matching query"
            )

            for file_reg in patient_files:
                logger.debug(
                    f"  Processing file: {file_reg.file_type}, path: {file_reg.file_path}, modality: {file_reg.modality}"
                )
                # Double-check: only export files that are in the explicitly requested file_types
                # list (handles ios_processed_upper / ios_processed_lower which don't end with
                # '_processed'), or any other processed/bite_classification file from a modality
                # relationship match.
                is_mapped_file_type = file_reg.file_type in file_types
                is_modality_fallback = (
                    file_reg.modality is not None
                    and file_reg.modality.slug in actual_modality_slugs
                    and self._file_type_matches_requested_content(file_reg.file_type)
                )
                if is_mapped_file_type or is_modality_fallback:
                    # Determine modality slug for file organization
                    if file_reg.modality:
                        modality_slug = file_reg.modality.slug
                    else:
                        # Infer from file_type if modality is not set
                        if file_reg.file_type.startswith("ios_"):
                            modality_slug = "ios"
                        elif file_reg.file_type.startswith("cbct_"):
                            modality_slug = "cbct"
                        elif file_reg.file_type.startswith("audio_"):
                            modality_slug = "audio"
                        elif file_reg.file_type.startswith("intraoral_"):
                            modality_slug = "intraoral-photo"
                        elif file_reg.file_type.startswith("teleradiography_"):
                            modality_slug = "teleradiography"
                        elif file_reg.file_type.startswith("panoramic_"):
                            modality_slug = "panoramic"
                        else:
                            modality_slug = None

                    logger.debug(
                        f"    Modality slug: {modality_slug}, file_path: {file_reg.file_path}, exists: {artifact_exists(file_reg.file_path) if file_reg.file_path else False}"
                    )

                    # Special handling for CBCT processed: files are stored in metadata['files']
                    if (
                        file_reg.file_type == "cbct_processed"
                        and file_reg.metadata
                        and "files" in file_reg.metadata
                    ):
                        # Export all files from metadata (volume_nifti, panoramic_view, etc.)
                        cbct_files = file_reg.metadata.get("files", {})
                        for file_type_key, file_data in cbct_files.items():
                            if isinstance(file_data, dict) and "path" in file_data:
                                file_path = file_data["path"]
                                if artifact_exists(file_path):
                                    files_to_export.append(
                                        {
                                            "type": "file",
                                            "patient": patient,
                                            "file_registry": file_reg,
                                            "path": file_path,
                                            "modality_slug": modality_slug,
                                            "cbct_file_type": file_type_key,  # e.g., 'volume_nifti', 'panoramic_view'
                                        }
                                    )
                                    # Use individual file size from metadata
                                    file_size = file_data.get("size", 0)
                                    total_size += file_size
                                else:
                                    logger.warning(
                                        f"CBCT processed file not found: {file_path}"
                                    )
                    elif file_reg.file_path and artifact_exists(file_reg.file_path):
                        # Standard single-file export
                        logger.info(
                            f"  Adding file: {file_reg.file_type} (modality: {modality_slug}) from {file_reg.file_path}"
                        )
                        files_to_export.append(
                            {
                                "type": "file",
                                "patient": patient,
                                "file_registry": file_reg,
                                "path": file_reg.file_path,
                                "modality_slug": modality_slug,
                            }
                        )
                        total_size += int(file_reg.file_size or 0)
                    elif not file_reg.file_path:
                        logger.warning(
                            f"FileRegistry {file_reg.id} ({file_reg.file_type}) has no file_path and no metadata files"
                        )
                    else:
                        logger.warning(
                            f"File not found: {file_reg.file_path} (file_type: {file_reg.file_type}, patient: {patient.patient_id})"
                        )
                else:
                    logger.debug(
                        f"  Skipping file {file_reg.file_type}: not in expected mapped types and not eligible modality fallback"
                    )

            # Collect VoiceCaption text files for reports (only if 'reports' is selected)
            if "reports" in self.modality_slugs:
                # When reports-only: use all active modalities; otherwise use selected modalities
                report_modality_slugs = [
                    s for s in self.modality_slugs if s != "reports"
                ]
                if not report_modality_slugs:
                    report_modality_slugs = list(
                        Modality.objects.filter(is_active=True).values_list(
                            "slug", flat=True
                        )
                    )
                for modality_slug in report_modality_slugs:
                    try:
                        modality = Modality.objects.get(slug=modality_slug)
                        voice_captions = VoiceCaption.objects.filter(
                            patient=patient,
                            modality=modality,
                            text_caption__isnull=False,
                        ).exclude(text_caption="")

                        for vc in voice_captions:
                            # Create a virtual file entry for the text caption (user_id = annotator for unique filename)
                            files_to_export.append(
                                {
                                    "type": "report",
                                    "patient": patient,
                                    "voice_caption": vc,
                                    "modality_slug": modality_slug,
                                    "content": vc.text_caption,
                                    "user_id": vc.user_id,
                                }
                            )
                            # Estimate text file size
                            total_size += len(vc.text_caption.encode("utf-8"))
                    except Modality.DoesNotExist:
                        pass

        logger.info(
            f"Total files collected: {len(files_to_export)}, total size: {total_size} bytes"
        )
        return files_to_export, total_size

    def create_zip(self, files_to_export, export_path):
        """Create ZIP file with structure: patient_{id}_{name}/modality/files and reports/."""
        os.makedirs(os.path.dirname(export_path), exist_ok=True)

        # Organize files by patient
        patient_files = {}
        for file_info in files_to_export:
            patient = file_info["patient"]
            patient_key = patient.patient_id

            if patient_key not in patient_files:
                patient_files[patient_key] = {
                    "patient": patient,
                    "files": [],
                    "reports": {},
                }

            if file_info["type"] == "file":
                patient_files[patient_key]["files"].append(file_info)
            elif file_info["type"] == "report":
                modality_slug = file_info["modality_slug"]
                if modality_slug not in patient_files[patient_key]["reports"]:
                    patient_files[patient_key]["reports"][modality_slug] = []
                patient_files[patient_key]["reports"][modality_slug].append(file_info)

        # Count total ZIP entries for progress
        total_entries = 0
        for patient_data in patient_files.values():
            modality_files = {}
            for file_info in patient_data["files"]:
                modality_slug = file_info.get("modality_slug") or "unknown"
                modality_files.setdefault(modality_slug, []).append(file_info)
            total_entries += sum(len(f) for f in modality_files.values())
            total_entries += sum(len(r) for r in patient_data["reports"].values())

        progress_interval = max(
            1, total_entries // 50
        )  # ~50 updates over the ZIP phase
        current_entry = [0]  # use list so inner closure can mutate

        with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            # Add files to ZIP
            for patient_key, patient_data in patient_files.items():
                patient = patient_data["patient"]

                # Create patient folder name
                if patient.name:
                    patient_folder = f"patient_{patient.patient_id}_{patient.name}"
                else:
                    patient_folder = f"patient_{patient.patient_id}"
                # Sanitize folder name (remove invalid characters)
                patient_folder = "".join(
                    c for c in patient_folder if c.isalnum() or c in ("_", "-", " ")
                )
                patient_folder = patient_folder.replace(" ", "_")

                # Group files by modality
                modality_files = {}
                for file_info in patient_data["files"]:
                    modality_slug = file_info.get("modality_slug") or "unknown"
                    if modality_slug not in modality_files:
                        modality_files[modality_slug] = []
                    modality_files[modality_slug].append(file_info)

                # Add files to ZIP organized by modality
                for modality_slug, files in modality_files.items():
                    for file_info in files:
                        file_reg = file_info["file_registry"]
                        source_path = file_info["path"]

                        if not artifact_exists(source_path):
                            logger.warning(f"Skipping missing file: {source_path}")
                            continue

                        # For CBCT files, use a descriptive filename based on file type
                        if modality_slug == "cbct" and "cbct_file_type" in file_info:
                            cbct_file_type = file_info["cbct_file_type"]
                            # Map CBCT file types to descriptive names
                            cbct_filename_map = {
                                "volume_nifti": "volume.nii.gz",
                                "panoramic_view": "panoramic.png",
                                "structures_mesh": "structures.stl",
                            }
                            # Handle multiple mesh files (structures_mesh_1, structures_mesh_2, etc.)
                            if cbct_file_type.startswith("structures_mesh"):
                                if cbct_file_type == "structures_mesh":
                                    filename = "structures.stl"
                                else:
                                    # Extract number if present (e.g., structures_mesh_1 -> structures_1.stl)
                                    suffix = cbct_file_type.replace(
                                        "structures_mesh_", ""
                                    )
                                    filename = (
                                        f"structures_{suffix}.stl"
                                        if suffix
                                        else "structures.stl"
                                    )
                            else:
                                filename = cbct_filename_map.get(
                                    cbct_file_type,
                                    os.path.basename((source_path or "").rstrip("/"))
                                    or "file",
                                )
                        else:
                            # Standard filename from path
                            filename = (
                                os.path.basename((source_path or "").rstrip("/"))
                                or "file"
                            )

                        # Create destination path: patient_folder/modality/filename
                        dest_path = f"{patient_folder}/{modality_slug}/{filename}"

                        try:
                            with zipf.open(dest_path, mode="w", force_zip64=True) as zf:
                                for chunk in iter_artifact_bytes(source_path):
                                    zf.write(chunk)
                        except Exception as e:
                            logger.error(f"Error adding file {source_path} to ZIP: {e}")
                        current_entry[0] += 1
                        if total_entries and current_entry[0] % progress_interval == 0:
                            pct = 20 + int(75 * current_entry[0] / total_entries)
                            self._update_progress(
                                f"Writing ZIP ({current_entry[0]}/{total_entries} files)",
                                pct,
                            )

                # Add report files (filename includes annotator user_id to avoid overwriting)
                for modality_slug, reports in patient_data["reports"].items():
                    for report_info in reports:
                        content = report_info["content"]
                        user_id = report_info.get("user_id", "unknown")
                        filename = f"{modality_slug}_{user_id}.txt"
                        dest_path = f"{patient_folder}/reports/{filename}"

                        # Write text content to ZIP
                        zipf.writestr(dest_path, content)
                        current_entry[0] += 1
                        if total_entries and current_entry[0] % progress_interval == 0:
                            pct = 20 + int(75 * current_entry[0] / total_entries)
                            self._update_progress(
                                f"Writing ZIP ({current_entry[0]}/{total_entries} files)",
                                pct,
                            )

        return os.path.getsize(export_path)

    def process_export(self):
        """Main processing method. Queries patients, collects files, creates ZIP, and updates export."""
        try:
            if self.has_content_selection and not (
                self.include_raw or self.include_processed
            ):
                self.export.mark_failed(
                    "No export content selected. Please enable Raw files and/or Processed files."
                )
                return

            # Query patients
            patients = self.query_patients()
            patient_count = patients.count()

            if patient_count == 0:
                self.export.mark_failed("No patients match the selected criteria.")
                return

            # Update patient count early so status API shows progress
            self.export.patient_count = patient_count
            self.export.save(update_fields=["patient_count"])
            self._update_progress(f"Collected {patient_count} patients", 5)

            # Collect files
            self._update_progress("Collecting files...", 10)
            files_to_export, estimated_size = self.collect_files(patients)

            if not files_to_export:
                self.export.mark_failed(self._build_no_files_found_error(patients))
                return

            # Generate filename
            timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
            filename = f"export_{self.export.id}_{timestamp}.zip"
            storage = get_object_storage()
            storage_key = f"exports/{filename}"

            self._update_progress("Writing ZIP...", 15)
            with tempfile.TemporaryDirectory(prefix="tf_export_") as tmpdir:
                export_path = os.path.join(tmpdir, filename)

                # Create ZIP (reports progress 20–95%)
                actual_size = self.create_zip(files_to_export, export_path)

                # Upload ZIP to object storage
                storage.upload_file(
                    export_path,
                    key=storage_key,
                    content_type="application/zip",
                    metadata={
                        "export_id": str(self.export.id),
                        "user_id": str(getattr(self.export, "user_id", "") or ""),
                    },
                )

                # Update export with results
                self.export.mark_completed(file_path=storage_key, file_size=actual_size)

            logger.info(
                f"Export {self.export.id} completed successfully. Size: {actual_size} bytes"
            )

        except Exception as e:
            logger.error(
                f"Error processing export {self.export.id}: {e}", exc_info=True
            )
            self.export.mark_failed(str(e))


def start_export_processing(export_id, domain="maxillo"):
    """Start background processing for an export in a subprocess.

    Uses a subprocess instead of a daemon thread so the export completes even
    after the HTTP request ends (web workers can recycle and kill threads).
    """
    from ..models import Export as MaxilloExport
    from brain.models import Export as BrainExport

    try:
        if domain == "brain":
            export = BrainExport.objects.filter(id=export_id).first()
        else:
            export = MaxilloExport.objects.filter(id=export_id).first()

        if not export:
            logger.error(f"Export {export_id} not found for domain {domain}")
            return

        export.mark_processing()

        # Run in a detached subprocess so it survives the request/worker
        base_dir = Path(settings.BASE_DIR)
        manage_py = base_dir / "manage.py"
        cmd = [
            sys.executable,
            str(manage_py),
            "run_export",
            str(export_id),
            "--domain",
            domain,
        ]
        subprocess.Popen(
            cmd,
            cwd=str(base_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info(f"Started background subprocess for export {export_id}")
    except (MaxilloExport.DoesNotExist, BrainExport.DoesNotExist):
        logger.error(f"Export {export_id} not found")
    except Exception as e:
        logger.error(f"Error starting export processing: {e}", exc_info=True)
        try:
            export = MaxilloExport.objects.filter(
                id=export_id
            ).first() or BrainExport.objects.get(id=export_id)
            export.mark_failed(str(e))
        except Exception:
            pass
