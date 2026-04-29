"""
Modality Processing Handlers

This module provides handlers for modality-specific processing logic when jobs complete or fail.
Instead of hardcoding modality checks in file_utils.py, each modality can have a registered handler.
"""

import logging
import json
from django.utils import timezone
from common.models import FileRegistry, Job
from common.file_access import exists as artifact_exists, open_binary
from common.object_storage import get_object_storage
from .models import Classification

logger = logging.getLogger(__name__)


class ModalityProcessingHandler:
    """Base class for modality-specific processing handlers."""

    def handle_job_completion(self, job, output_files, logs=None):
        """
        Handle job completion for this modality.

        Args:
            job: The Job instance that completed
            output_files: Dict of output file types to paths
            logs: Optional logs/transcription text

        Returns:
            bool: True if handled successfully, False otherwise
        """
        return False

    def handle_job_failure(self, job, error_msg):
        """
        Handle job failure for this modality.

        Args:
            job: The Job instance that failed
            error_msg: Error message

        Returns:
            bool: True if handled successfully, False otherwise
        """
        return False


class CBCTHandler(ModalityProcessingHandler):
    """Handler for CBCT processing."""

    def handle_job_completion(self, job, output_files, logs=None):
        from .file_utils import get_file_type_for_modality

        if not job.patient:
            return False

        # Clean up existing processed CBCT files
        cbct_processed_type = get_file_type_for_modality("cbct", is_processed=True)
        FileRegistry.objects.filter(
            patient=job.patient, file_type=cbct_processed_type
        ).delete()

        # Register multi-file output
        processed_files = {}
        total_size = 0

        for file_type, file_path in output_files.items():
            if artifact_exists(file_path):
                try:
                    info = get_object_storage().head(file_path)
                except Exception:
                    continue
                file_hash = info.etag or ""
                file_size = int(info.content_length or 0)
                total_size += file_size
                processed_files[file_type] = {
                    "path": file_path,
                    "size": file_size,
                    "hash": file_hash,
                    "type": file_type,
                }

        # Create FileRegistry entry
        from common.models import Modality

        modality = Modality.objects.filter(slug="cbct").first()

        FileRegistry.objects.create(
            patient=job.patient,
            modality=modality,
            file_type=cbct_processed_type,
            file_path="",  # Multi-file, no single path
            file_size=total_size,
            file_hash="multi-file",
            processing_job=job,
            metadata={
                "files": processed_files,
                "processed_at": timezone.now().isoformat(),
                "logs": logs or "",
            },
        )

        # Update patient status
        job.patient.cbct_processing_status = "processed"
        job.patient.save()

        return True

    def handle_job_failure(self, job, error_msg):
        if job.patient:
            job.patient.cbct_processing_status = "failed"
            job.patient.save()
        return True


class IOSHandler(ModalityProcessingHandler):
    """Handler for IOS processing."""

    def handle_job_completion(self, job, output_files, logs=None):
        from .file_utils import get_file_type_for_modality

        if not job.patient:
            return False

        # Register output files for IOS
        from common.models import Modality

        modality = Modality.objects.filter(slug="ios").first()

        for file_type, file_path in output_files.items():
            if artifact_exists(file_path):
                try:
                    info = get_object_storage().head(file_path)
                except Exception:
                    continue
                FileRegistry.objects.create(
                    patient=job.patient,
                    modality=modality,
                    file_type=get_file_type_for_modality(
                        "ios",
                        is_processed=True,
                        subtype=file_type.replace("_processed", ""),
                    ),
                    file_path=file_path,
                    file_size=int(info.content_length or 0),
                    file_hash=info.etag or "",
                    processing_job=job,
                    metadata={"processed_at": timezone.now().isoformat()},
                )

        # Update patient status
        job.patient.ios_processing_status = "processed"
        job.patient.save()

        # Update dependent bite classification jobs
        dependent_bite_jobs = job.dependent_jobs.filter(
            modality_slug="bite_classification"
        )
        for bite_job in dependent_bite_jobs:
            if output_files:
                bite_job.input_file_path = json.dumps(output_files)
                bite_job.save()

        return True

    def handle_job_failure(self, job, error_msg):
        if job.patient:
            job.patient.ios_processing_status = "failed"
            job.patient.save()
        return True


class AudioHandler(ModalityProcessingHandler):
    """Handler for audio/voice processing."""

    def handle_job_completion(self, job, output_files, logs=None):
        if not job.voice_caption:
            return False

        job.voice_caption.processing_status = "completed"

        # Extract transcription
        if logs and isinstance(logs, str) and logs.strip():
            job.voice_caption.text_caption = logs.strip()
        else:
            # Try to extract from output files
            for file_path in output_files.values():
                if file_path.endswith(".txt"):
                    try:
                        fh, _ = open_binary(file_path)
                        with fh:
                            text_content = (
                                fh.read().decode("utf-8", errors="ignore").strip()
                            )
                            if text_content:
                                job.voice_caption.text_caption = text_content
                                break
                    except Exception as e:
                        logger.error(f"Error reading text file {file_path}: {e}")

        job.voice_caption.save_original_transcription()
        job.voice_caption.save()

        return True

    def handle_job_failure(self, job, error_msg):
        if job.voice_caption:
            job.voice_caption.processing_status = "failed"
            job.voice_caption.save()
        return True


class BiteClassificationHandler(ModalityProcessingHandler):
    """Handler for bite classification processing."""

    def handle_job_completion(self, job, output_files, logs=None):
        if not job.patient:
            return False

        # Parse classification from logs or output files
        classification_data = {}
        if logs:
            try:
                classification_data = json.loads(logs)
            except (json.JSONDecodeError, TypeError):
                pass

        if not classification_data:
            for file_path in output_files.values():
                if file_path.endswith(".json"):
                    try:
                        fh, _ = open_binary(file_path)
                        with fh:
                            classification_data = json.loads(
                                fh.read().decode("utf-8", errors="ignore")
                            )
                            break
                    except Exception:
                        pass

        if classification_data:
            # Create or update pipeline classification
            Classification.objects.update_or_create(
                patient=job.patient,
                classifier="pipeline",
                defaults={
                    "sagittal_left": classification_data.get(
                        "sagittal_left", "Unknown"
                    ),
                    "sagittal_right": classification_data.get(
                        "sagittal_right", "Unknown"
                    ),
                    "vertical": classification_data.get("vertical", "Unknown"),
                    "transverse": classification_data.get("transverse", "Unknown"),
                    "midline": classification_data.get("midline", "Unknown"),
                },
            )

        return True


# Registry of handlers by modality slug
_HANDLER_REGISTRY = {}


def register_handler(modality_slug: str, handler: ModalityProcessingHandler):
    """Register a processing handler for a modality."""
    _HANDLER_REGISTRY[modality_slug] = handler


def get_handler(modality_slug: str) -> Optional[ModalityProcessingHandler]:
    """Get the processing handler for a modality."""
    return _HANDLER_REGISTRY.get(modality_slug)


def handle_modality_job_completion(job, output_files, logs=None):
    """
    Handle job completion using the registered handler for the job's modality.

    Args:
        job: The Job instance
        output_files: Dict of output files
        logs: Optional logs/transcription

    Returns:
        bool: True if handled by a specific handler, False if generic handling needed
    """
    handler = get_handler(job.modality_slug)
    if handler:
        return handler.handle_job_completion(job, output_files, logs)
    return False


def handle_modality_job_failure(job, error_msg):
    """
    Handle job failure using the registered handler for the job's modality.

    Args:
        job: The Job instance
        error_msg: Error message

    Returns:
        bool: True if handled by a specific handler, False if generic handling needed
    """
    handler = get_handler(job.modality_slug)
    if handler:
        return handler.handle_job_failure(job, error_msg)
    return False


# Register built-in handlers
register_handler("cbct", CBCTHandler())
register_handler("ios", IOSHandler())
register_handler("audio", AudioHandler())
register_handler("voice", AudioHandler())  # Voice uses same handler as audio
register_handler("bite_classification", BiteClassificationHandler())


# Optional import fix for type hint
from typing import Optional
