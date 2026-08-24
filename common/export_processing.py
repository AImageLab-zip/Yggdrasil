"""Export processor for background export generation."""

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from common import export_catalog
from common.file_access import exists as artifact_exists
from common.file_access import iter_bytes as iter_artifact_bytes
from common.object_storage import get_object_storage
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)


def build_patient_classification_blob(patient, classifications):
    """Build a JSON-serializable summary of a patient's bite classification.

    `classifications` is an iterable of 0-2 Classification rows (one per
    classifier: 'manual' and/or 'pipeline'). Returns None if there is
    nothing to export for this patient.
    """
    by_classifier = {c.classifier: c for c in classifications}
    manual, pipeline = by_classifier.get("manual"), by_classifier.get("pipeline")
    if not manual and not pipeline:
        return None

    def _serialize(c):
        if c is None:
            return None
        return {
            "sagittal_left": {
                "code": c.sagittal_left,
                "label": c.get_sagittal_left_display(),
            },
            "sagittal_right": {
                "code": c.sagittal_right,
                "label": c.get_sagittal_right_display(),
            },
            "vertical": {"code": c.vertical, "label": c.get_vertical_display()},
            "transverse": {
                "code": c.transverse,
                "label": c.get_transverse_display(),
            },
            "midline": {"code": c.midline, "label": c.get_midline_display()},
            "annotator": c.annotator.username if c.annotator_id else None,
            "timestamp": c.timestamp.isoformat() if c.timestamp else None,
        }

    return {
        "patient_id": patient.patient_id,
        "manual": _serialize(manual),
        "pipeline": _serialize(pipeline),
    }


class ExportProcessor:
    """Processes export jobs by querying patients, collecting files, and creating ZIP archives."""

    def __init__(self, export, domain="maxillo"):
        """Initialize processor with export instance."""
        self.export = export
        self.domain = domain
        # Which of FileRegistry's parallel patient FK columns this domain uses.
        # The modality -> file_type mapping that used to live here (and in two
        # other copies) is now common.export_catalog.
        self.patient_fk = "brain_patient" if domain == "brain" else "patient"
        self.query_params = export.query_params
        self.folder_ids = self.query_params.get("folder_ids", [])
        self.project_id = self.query_params.get("project_id")

        # Artifact selection (common.export_catalog) is the single source of
        # truth for what an export contains. Rows written before artifacts
        # existed carry modality_slugs + include_* flags instead, and are
        # translated so they keep re-running identically.
        self.artifact_keys = list(self.query_params.get("artifacts") or [])
        if self.artifact_keys:
            self.artifacts = export_catalog.resolve_artifacts(domain, self.artifact_keys)
        else:
            self.artifacts = self._legacy_artifacts()
        self.modality_slugs = sorted(export_catalog.modality_slugs_for(self.artifacts))
        self.collectors = export_catalog.collectors_for(self.artifacts)
        self.filters = export_catalog.normalize_filters(
            self.query_params.get("filters", {})
        )

    def _legacy_artifacts(self):
        """Artifacts for an Export row predating the artifact selection."""
        raw_slugs = self.query_params.get("modality_slugs", [])
        declares_content = (
            "include_raw" in self.query_params or "include_processed" in self.query_params
        )
        if declares_content:
            include_raw = self._coerce_bool(self.query_params.get("include_raw"))
            include_processed = self._coerce_bool(self.query_params.get("include_processed"))
        else:
            # Older still: content selection did not exist, everything was in.
            include_raw = include_processed = True
        return export_catalog.artifacts_from_legacy_selection(
            self.domain,
            [slug for slug in raw_slugs if slug != "reports"],
            include_raw=include_raw,
            include_processed=include_processed,
            # "reports" used to be a pseudo modality slug before it was a flag.
            include_reports=self._coerce_bool(
                self.query_params.get("include_reports"),
                default="reports" in raw_slugs,
            ),
            include_bite_classification=self._coerce_bool(
                self.query_params.get("include_bite_classification")
            ),
        )

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

    def _patient_file_queryset(self, patients):
        from common.models import FileRegistry

        return FileRegistry.objects.filter(
            domain=self.domain, **{f"{self.patient_fk}__in": patients}
        )

    def _build_no_files_found_error(self, patients):
        """Explain an empty export in terms of what was actually asked for."""
        if not self.artifacts:
            return (
                "No export content selected. Choose at least one artifact "
                "(raw files, processed outputs, reports, ...)."
            )

        rows = self._patient_file_queryset(patients)
        availability = []
        for artifact in self.artifacts:
            if not artifact.is_file_backed:
                availability.append(f"{artifact.key}(database)")
                continue
            count = rows.filter(artifact.registry_q()).count()
            availability.append(f"{artifact.key}({count})")

        message = (
            f"No files found for {patients.count()} matching patient(s). "
            f"Requested: {', '.join(a.key for a in self.artifacts)}."
        )
        if availability:
            message += f" Availability: {'; '.join(availability)}."
        processed_only = all(
            artifact.bucket != export_catalog.BUCKET_RAW for artifact in self.artifacts
        )
        if processed_only:
            message += " Tip: include a raw artifact if processing has not finished yet."
        return message

    def _domain_models(self):
        """Return (Patient, VoiceCaption) model classes for the active domain."""
        if self.domain == "brain":
            from brain.models import Patient, VoiceCaption
        else:
            from maxillo.models import Patient, VoiceCaption
        return Patient, VoiceCaption

    def _filter_patients_by_folders(self, Patient):
        """Base patient queryset restricted to the requested folders.

        Selecting a folder includes its sub-folders: folders nest, and picking a
        parent while silently dropping everything under it would be a
        surprising, hard-to-notice hole in the export.

        Every domain links a patient to one folder via the `folder` FK. Brain used
        to use a `folders` many-to-many, and this method still queried it long
        after the folder->project migration collapsed it to a single FK -- which
        made every brain export fail with a FieldError.
        """
        if not self.folder_ids:
            return Patient.objects.none()
        return Patient.objects.filter(folder_id__in=self._folder_closure(Patient))

    def _folder_closure(self, Patient):
        """The selected folders plus every descendant, as a list of ids."""
        folder_model = Patient._meta.get_field("folder").related_model
        selected = [int(folder_id) for folder_id in self.folder_ids]
        closure = set(selected)
        frontier = selected
        # Folder trees here are shallow; a handful of breadth-first queries beats
        # a recursive CTE for readability, and the loop is bounded by depth.
        while frontier:
            children = list(
                folder_model.objects.filter(parent_id__in=frontier)
                .exclude(id__in=closure)
                .values_list("id", flat=True)
            )
            if not children:
                break
            closure.update(children)
            frontier = children
        return sorted(closure)

    def _update_progress(self, message, percent=None):
        """Update progress on the Export record for live feedback."""
        Export = self.export.__class__

        update_kw = {"progress_message": message}
        if percent is not None:
            update_kw["progress_percent"] = min(100, max(0, int(percent)))
        Export.objects.filter(pk=self.export.pk).update(**update_kw)

    def query_patients(self):
        """Patients in the requested folders, narrowed by the active filters.

        Filter application lives in ``common.export_catalog.apply_filters`` so the
        preview endpoint counts exactly what the ZIP will contain.
        """
        Patient, _VoiceCaption = self._domain_models()

        patients = self._filter_patients_by_folders(Patient)
        if not patients.exists():
            return patients

        return export_catalog.apply_filters(
            patients, self.domain, self.filters, artifacts=self.artifacts
        )

    def collect_files(self, patients):
        """Resolve the selected artifacts into concrete ZIP entries.

        One pass per patient. File-backed artifacts are matched against the
        patient's FileRegistry rows (including outputs nested in a bundle row's
        ``metadata['files']``, which is how CBCT completions publish the volume,
        the segmentation and the inference stats); database-backed artifacts are
        produced by the collectors below.
        """
        from common.models import FileRegistry

        file_artifacts = [a for a in self.artifacts if a.is_file_backed]
        collector_artifacts = [a for a in self.artifacts if a.collector]

        entries = []
        total_size = 0

        registry_query = None
        for artifact in file_artifacts:
            registry_query = (
                artifact.registry_q() if registry_query is None
                else registry_query | artifact.registry_q()
            )

        logger.info(
            "Collecting %d artifact(s) for %d patient(s): %s",
            len(self.artifacts), patients.count(),
            ", ".join(a.key for a in self.artifacts) or "none",
        )

        for patient in patients:
            if registry_query is not None:
                rows = (
                    FileRegistry.objects.filter(
                        domain=self.domain, **{self.patient_fk: patient}
                    )
                    .filter(registry_query)
                    .distinct()
                )
                for row in rows:
                    for artifact in file_artifacts:
                        if not artifact.matches(row):
                            continue
                        entry, size = self._file_entry(patient, artifact, row)
                        if entry is not None:
                            entries.append(entry)
                            total_size += size

            for artifact in collector_artifacts:
                for entry, size in self._collect_documents(patient, artifact):
                    entries.append(entry)
                    total_size += size

        logger.info("Total entries collected: %d, total size: %d bytes", len(entries), total_size)
        return entries, total_size

    def _file_entry(self, patient, artifact, row):
        """Build one ZIP entry for a FileRegistry row, or (None, 0) if unusable."""
        output = artifact.resolve_output(row)
        path = output["path"] if output else None
        size = output["size"] if output else 0

        if not path:
            logger.warning(
                "FileRegistry %s (%s) has no path for artifact %s",
                row.id, row.file_type, artifact.key,
            )
            return None, 0
        if not artifact_exists(path):
            logger.warning("File not found for artifact %s: %s", artifact.key, path)
            return None, 0

        return {
            "type": "file",
            "patient": patient,
            "artifact": artifact,
            "file_registry": row,
            "path": path,
        }, size

    def _collect_documents(self, patient, artifact):
        """Yield ``(entry, size)`` for a database-backed artifact."""
        producer = {
            "captions": self._collect_captions,
            "occlusion": self._collect_occlusion,
            "tooth_segmentation": self._collect_tooth_segmentation,
        }.get(artifact.collector)
        if producer is None:
            logger.warning("No collector registered for artifact %s", artifact.key)
            return
        yield from producer(patient, artifact)

    def _collect_captions(self, patient, artifact):
        _Patient, VoiceCaption = self._domain_models()
        captions = VoiceCaption.objects.filter(
            patient=patient, text_caption__isnull=False
        ).exclude(text_caption="")
        # Restrict to the modalities being exported when the selection names
        # any, so an IOS-only export does not carry CBCT dictations.
        if self.modality_slugs:
            captions = captions.filter(modality__in=self.modality_slugs)
        for caption in captions:
            content = caption.text_caption
            yield (
                {
                    "type": "document",
                    "patient": patient,
                    "artifact": artifact,
                    "content": content,
                    "filename": f"{caption.modality or 'patient'}_{caption.user_id or 'unknown'}_{caption.id}.txt",
                },
                len(content.encode("utf-8")),
            )

    def _collect_occlusion(self, patient, artifact):
        if self.domain != "maxillo":
            return
        from maxillo.models import Classification

        classifications = list(
            Classification.objects.filter(patient=patient).select_related("annotator")
        )
        blob = build_patient_classification_blob(patient, classifications)
        if blob is None:
            return
        content = json.dumps(blob, indent=2)
        yield (
            {
                "type": "document",
                "patient": patient,
                "artifact": artifact,
                "content": content,
                "filename": artifact.filename or "classification.json",
            },
            len(content.encode("utf-8")),
        )

    def _collect_tooth_segmentation(self, patient, artifact):
        if self.domain != "maxillo":
            return
        from maxillo.models import IntraoralToothSegmentation

        rows = IntraoralToothSegmentation.objects.filter(patient=patient).select_related(
            "image_file"
        )
        for row in rows:
            image_name = os.path.basename((row.image_file.file_path or "").rstrip("/")) or str(
                row.image_file_id
            )
            blob = {
                "patient_id": patient.patient_id,
                "image_file_id": row.image_file_id,
                "image": image_name,
                "is_confirmed": row.is_confirmed,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "teeth": row.teeth or {},
            }
            content = json.dumps(blob, indent=2)
            stem = os.path.splitext(image_name)[0]
            yield (
                {
                    "type": "document",
                    "patient": patient,
                    "artifact": artifact,
                    "content": content,
                    "filename": f"{stem}.json",
                },
                len(content.encode("utf-8")),
            )

    @staticmethod
    def _patient_folder(patient):
        """`patient_<id>_<name>` with anything path-unsafe stripped."""
        name = f"patient_{patient.patient_id}_{patient.name}" if patient.name else f"patient_{patient.patient_id}"
        name = "".join(c for c in name if c.isalnum() or c in ("_", "-", " "))
        return name.replace(" ", "_")

    @staticmethod
    def _entry_filename(entry):
        """Name this entry takes inside the ZIP."""
        artifact = entry["artifact"]
        if entry["type"] == "document":
            return entry["filename"]
        # A bundled output (CBCT volume / segmentation / stats) is stored under an
        # opaque object key, so the artifact supplies the readable name.
        if artifact.nested_key and artifact.filename:
            return artifact.filename
        if artifact.filename:
            return artifact.filename
        return os.path.basename((entry["path"] or "").rstrip("/")) or "file"

    def create_zip(self, entries, export_path):
        """Write the ZIP: ``patient_<id>_<name>/<artifact directory>/<filename>``.

        Directory layout comes from each artifact (``Artifact.zip_directory``), so
        raw/processed stay where they always were while derived outputs get their
        own readable folders (``panoramic/generated``, ``ios/landmarks``, ...).
        """
        os.makedirs(os.path.dirname(export_path), exist_ok=True)

        total_entries = len(entries)
        progress_interval = max(1, total_entries // 50)  # ~50 updates over the ZIP phase
        written = 0
        # A patient can hold two artifacts that resolve to the same filename
        # (e.g. both IOS arches under one name); keep the archive lossless.
        used_paths = set()

        with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for entry in entries:
                artifact = entry["artifact"]
                directory = f"{self._patient_folder(entry['patient'])}/{artifact.zip_directory()}"
                filename = self._entry_filename(entry)
                dest_path = self._unique_path(used_paths, directory, filename)

                try:
                    if entry["type"] == "document":
                        zipf.writestr(dest_path, entry["content"])
                    else:
                        source_path = entry["path"]
                        if not artifact_exists(source_path):
                            logger.warning("Skipping missing file: %s", source_path)
                            continue
                        with zipf.open(dest_path, mode="w", force_zip64=True) as handle:
                            for chunk in iter_artifact_bytes(source_path):
                                handle.write(chunk)
                except Exception as exc:  # noqa: BLE001 - one bad file must not kill the export
                    logger.error("Error adding %s to ZIP: %s", dest_path, exc)
                    continue

                written += 1
                if total_entries and written % progress_interval == 0:
                    percent = 20 + int(75 * written / total_entries)
                    self._update_progress(
                        f"Writing ZIP ({written}/{total_entries} files)", percent
                    )

        return os.path.getsize(export_path)

    @staticmethod
    def _unique_path(used_paths, directory, filename):
        candidate = f"{directory}/{filename}"
        if candidate not in used_paths:
            used_paths.add(candidate)
            return candidate
        stem, extension = os.path.splitext(filename)
        index = 2
        while f"{directory}/{stem}_{index}{extension}" in used_paths:
            index += 1
        candidate = f"{directory}/{stem}_{index}{extension}"
        used_paths.add(candidate)
        return candidate

    def process_export(self):
        """Main processing method. Queries patients, collects files, creates ZIP, and updates export."""
        try:
            if not self.artifacts:
                self.export.mark_failed(
                    "No export content selected. Choose at least one artifact to export."
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

    from brain.models import Export as BrainExport
    from laparoscopy.models import Export as LaparoscopyExport
    from maxillo.models import Export as MaxilloExport
    try:
        if domain == "laparoscopy":
            export = LaparoscopyExport.objects.filter(id=export_id).first()
        elif domain == "brain":
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
    except MaxilloExport.DoesNotExist:
        logger.error(f"Export {export_id} not found")
    except Exception as e:
        logger.error(f"Error starting export processing: {e}", exc_info=True)
        try:
            export = (
                MaxilloExport.objects.filter(id=export_id).first()
                or LaparoscopyExport.objects.filter(id=export_id).first()
                or BrainExport.objects.filter(id=export_id).first()
            )
            if export:
                export.mark_failed(str(e))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared export view-layer helpers (promoted from maxillo.views.export, Phase 5.1)
# Domain-agnostic: they operate on a passed Export instance / request / id.
# ---------------------------------------------------------------------------


def build_shared_download_url(request, share_token):
    """Build absolute shared landing URL for an export token."""
    namespace = (
        getattr(request, "resolver_match", None) and request.resolver_match.namespace
    ) or "maxillo"
    return request.build_absolute_uri(
        reverse(
            f"{namespace}:export_shared_landing",
            kwargs={"share_token": share_token},
        )
    )


def recover_stuck_export(export):
    """
    If export is stuck in 'processing' but a completed ZIP exists in object
    storage (process died before DB update), mark it as completed.
    """
    if export.status != "processing":
        return export
    try:
        storage = get_object_storage()

        if export.file_path and artifact_exists(export.file_path):
            info = storage.head(export.file_path)
            size = int(info.content_length or 0)
            export.mark_completed(file_path=export.file_path, file_size=size)
            export.refresh_from_db()
            logger.info(
                "Recovered stuck export %s: marked completed from key %s",
                export.id,
                export.file_path,
            )
            return export

        prefix = f"exports/export_{export.id}_"
        candidates = [
            key
            for key in storage.list_keys(prefix)
            if key.startswith(prefix) and key.endswith(".zip")
        ]
        if not candidates:
            return export

        key = sorted(candidates)[-1]
        info = storage.head(key)
        size = int(info.content_length or 0)
        export.mark_completed(file_path=key, file_size=size)
        export.refresh_from_db()
        logger.info(
            "Recovered stuck export %s: marked completed from key %s",
            export.id,
            key,
        )
    except Exception as e:
        logger.warning(f"Could not recover export {export.id}: {e}")
    return export


def kill_export_processes(export_id):
    """Best-effort kill of run_export worker process(es) for one export id."""
    killed_pids = []
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"manage.py run_export {int(export_id)}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return killed_pids

        for line in (result.stdout or "").splitlines():
            line = (line or "").strip()
            if not line:
                continue
            try:
                pid = int(line)
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                killed_pids.append(pid)
            except ProcessLookupError:
                continue
            except Exception:
                continue
    except Exception:
        return killed_pids

    # Escalate to SIGKILL if still alive after short grace period
    if killed_pids:
        time.sleep(0.8)
        for pid in list(killed_pids):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except Exception:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    return killed_pids


def format_file_size(size_bytes):
    """Format file size in human-readable format."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
