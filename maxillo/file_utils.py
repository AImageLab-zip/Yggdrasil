import os
import hashlib
import contextlib
import logging
import traceback
from pathlib import Path
import tempfile
from django.utils import timezone
from django.db import transaction
from common.models import FileRegistry, Job
from .models import VoiceCaption, Classification, Patient
import json
import zipfile
import tarfile

logger = logging.getLogger(__name__)

from common.file_access import exists as artifact_exists, open_binary
from common.object_storage import get_object_storage


def get_file_type_for_modality(
    modality_slug, is_processed=False, file_format=None, subtype=None
):
    """
    Centralized function to determine the correct file_type for a given modality.

    Args:
        modality_slug: The modality slug (e.g., 'cbct', 'ios', 'braintumor-mri-t1')
        is_processed: Whether this is a processed file (adds _processed suffix)
        file_format: Optional file format hint for fallback logic
        subtype: Optional subtype (e.g., 'upper', 'lower' for IOS)

    Returns:
        str: The file_type to use in FileRegistry
    """
    from common.models import FileRegistry

    if not modality_slug:
        return "generic_processed" if is_processed else "generic_raw"

    # Special handling for IOS with subtypes
    if modality_slug == "ios" and subtype:
        base_type = f"ios_{subtype}"
        file_type = f"{base_type}_processed" if is_processed else f"{base_type}_raw"
        valid_file_types = FileRegistry.get_file_type_choices_dict().keys()
        if file_type in valid_file_types:
            return file_type

    # Convert modality slug to file_type by replacing hyphens with underscores
    base_modality = modality_slug.replace("-", "_")
    suffix = "_processed" if is_processed else "_raw"
    potential_file_type = base_modality + suffix

    # Check if this file_type exists in our choices
    valid_file_types = FileRegistry.get_file_type_choices_dict().keys()

    if potential_file_type in valid_file_types:
        return potential_file_type

    # Fallback mappings for special cases
    fallback_mappings = {
        "cbct": "cbct_raw" if not is_processed else "cbct_processed",
        "ios": "cbct_raw"
        if not is_processed
        else "cbct_processed",  # Keep existing behavior
        "audio": "audio_raw" if not is_processed else "audio_processed",
        "bite_classification": "bite_classification",  # Special case - no raw/processed distinction
        "intraoral": "intraoral_raw" if not is_processed else "intraoral_processed",
        "teleradiography": "teleradiography_raw"
        if not is_processed
        else "teleradiography_processed",
        "panoramic": "panoramic_raw" if not is_processed else "panoramic_processed",
        "rawzip": "generic_raw"
        if not is_processed
        else "generic_processed",  # RawZip files use generic types
    }

    if modality_slug in fallback_mappings:
        return fallback_mappings[modality_slug]

    # File format-based fallbacks for unknown modalities
    if not is_processed and file_format:
        if file_format in ["nii", "nii.gz", "dicom", "mha", "mhd", "nrrd"]:
            return "volume_raw"
        elif file_format in ["jpg", "jpeg", "png", "bmp", "tiff", "tif"]:
            return "image_raw"

    # Final fallback
    return "generic_processed" if is_processed else "generic_raw"


def _get_patient(obj):
    """Resolve a Patient instance from various inputs (Patient, VoiceCaption with patient, legacy scanpair)."""
    if hasattr(obj, "_meta") and getattr(obj._meta, "model_name", "") == "patient":
        return obj
    if hasattr(obj, "patient") and getattr(obj, "patient") is not None:
        return getattr(obj, "patient")
    raise ValueError("Cannot resolve Patient from object")


def _domain_for_patient(patient) -> str:
    app_label = getattr(getattr(patient, "_meta", None), "app_label", "")
    if app_label == "brain":
        return "brain"
    return "maxillo"


def _entity_fk_kwargs(patient):
    domain = _domain_for_patient(patient)
    if domain == "brain":
        return {
            "domain": "brain",
            "brain_patient": patient,
            "patient": None,
        }
    return {
        "domain": "maxillo",
        "patient": patient,
        "brain_patient": None,
    }


def _entity_filter_kwargs(patient):
    domain = _domain_for_patient(patient)
    if domain == "brain":
        return {
            "domain": "brain",
            "brain_patient": patient,
        }
    return {
        "domain": "maxillo",
        "patient": patient,
    }


def _voice_entity_fk_kwargs(voice_caption):
    patient = _get_patient(voice_caption)
    domain = _domain_for_patient(patient)
    if domain == "brain":
        return {
            "brain_voice_caption": voice_caption,
            "voice_caption": None,
        }
    return {
        "voice_caption": voice_caption,
        "brain_voice_caption": None,
    }


def _project_slug_from_patient(patient) -> str:
    domain = _domain_for_patient(patient)
    return "brain" if domain == "brain" else "maxillo"


def _domain_for_job(job) -> str:
    if getattr(job, "domain", None) in ["brain", "maxillo"]:
        return job.domain
    if getattr(job, "brain_patient_id", None) or getattr(
        job, "brain_voice_caption_id", None
    ):
        return "brain"
    return "maxillo"


def _job_patient(job):
    return (
        getattr(job, "brain_patient", None)
        if _domain_for_job(job) == "brain"
        else getattr(job, "patient", None)
    )


def _job_voice_caption(job):
    return (
        getattr(job, "brain_voice_caption", None)
        if _domain_for_job(job) == "brain"
        else getattr(job, "voice_caption", None)
    )


def _job_entity_fk_kwargs(job):
    if _domain_for_job(job) == "brain":
        return {
            "domain": "brain",
            "brain_patient": _job_patient(job),
            "patient": None,
            "brain_voice_caption": _job_voice_caption(job),
            "voice_caption": None,
        }
    return {
        "domain": "maxillo",
        "patient": _job_patient(job),
        "brain_patient": None,
        "voice_caption": _job_voice_caption(job),
        "brain_voice_caption": None,
    }


def _raw_key_prefix_for(patient: Patient, modality_slug: str) -> str:
    project_slug = _project_slug_from_patient(patient)
    return f"{project_slug}/raw/{modality_slug}".strip("/")


def _processed_key_prefix_for(patient: Patient, modality_slug: str) -> str:
    project_slug = _project_slug_from_patient(patient)
    return f"{project_slug}/processed/{modality_slug}".strip("/")


def _sanitize_relpath(p: str) -> str:
    p = (p or "").lstrip("/").replace("\\", "/")
    parts = [seg for seg in p.split("/") if seg and seg not in {".", ".."}]
    return "/".join(parts)


def _upload_uploaded_file_to_storage(
    *, key: str, uploaded_file
) -> tuple[str, int, str]:
    storage = get_object_storage()

    fd, tmp_path = tempfile.mkstemp(prefix="tf_upload_")
    os.close(fd)
    try:
        hash_sha256 = hashlib.sha256()
        size = 0
        with open(tmp_path, "wb+") as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)
                hash_sha256.update(chunk)
                size += len(chunk)

        storage.upload_file(tmp_path, key=key)
        return key, size, hash_sha256.hexdigest()
    finally:
        with contextlib.suppress(Exception):
            os.remove(tmp_path)


def _resolve_output_path_or_key(out_spec):
    if isinstance(out_spec, str):
        return out_spec
    if isinstance(out_spec, dict):
        return out_spec.get("path") or out_spec.get("key")
    return None


def _size_hash_for_path_or_key(path_or_key):
    if not path_or_key:
        return None, None
    try:
        info = get_object_storage().head(path_or_key)
        return info.content_length, info.etag
    except Exception:
        return None, None


def _detect_extension_and_format(filename_lower: str):
    if filename_lower.endswith(".nii.gz"):
        return ".nii.gz", "nifti_compressed"
    if filename_lower.endswith(".nii"):
        return ".nii", "nifti"
    if filename_lower.endswith((".dcm", ".dicom")):
        return ".dcm", "dicom_single"
    if filename_lower == "dicomdir" or filename_lower.endswith("/dicomdir"):
        return "", "dicomdir"
    if filename_lower.endswith(".mha"):
        return ".mha", "metaimage"
    if filename_lower.endswith(".mhd"):
        return ".mhd", "metaimage_header"
    if filename_lower.endswith(".nrrd"):
        return ".nrrd", "nrrd"
    if filename_lower.endswith(".nhdr"):
        return ".nhdr", "nrrd_header"
    if filename_lower.endswith(".zip"):
        return ".zip", "dicom_archive_zip"
    if filename_lower.endswith((".tar", ".tar.gz", ".tgz")):
        if filename_lower.endswith(".tar.gz"):
            return ".tar.gz", "dicom_archive_tar"
        if filename_lower.endswith(".tgz"):
            return ".tgz", "dicom_archive_tar"
        return ".tar", "dicom_archive_tar"
    # Fallback
    return os.path.splitext(filename_lower)[1] or ".bin", "unknown"


def save_generic_modality_file(
    patient: Patient, modality_slug: str, uploaded_file, job=False
):
    """Save a single modality file to object storage and create a Job."""
    original_name = uploaded_file.name
    extension, file_format = _detect_extension_and_format(original_name.lower())
    filename = f"{modality_slug}_patient_{patient.patient_id}{extension}"
    key_prefix = _raw_key_prefix_for(patient, modality_slug)
    key = f"{key_prefix}/{filename}"
    key, file_size, file_hash = _upload_uploaded_file_to_storage(
        key=key, uploaded_file=uploaded_file
    )
    # Resolve modality FK for FileRegistry
    modality_fk = None
    try:
        from common.models import Modality as _Modality

        modality_fk = _Modality.objects.filter(slug=modality_slug).first()
    except Exception:
        modality_fk = None
    # Determine appropriate file_type using centralized function
    file_type = get_file_type_for_modality(
        modality_slug, is_processed=False, file_format=file_format
    )

    try:
        fr = FileRegistry.objects.create(
            file_type=file_type,
            file_path=key,
            file_size=file_size,
            file_hash=file_hash,
            **_entity_fk_kwargs(patient),
            modality=modality_fk,
            metadata={
                "original_filename": original_name,
                "uploaded_at": timezone.now().isoformat(),
                "file_format": file_format,
                "modality_slug": modality_slug,
            },
        )
    except Exception:
        logger.exception(
            "Failed to create FileRegistry for %s; proceeding to create Job anyway",
            modality_slug,
        )
        fr = None

    # Create job (completed for image modalities that don't need processing)
    job_obj = None
    try:
        # Image modalities that don't need processing
        no_processing_modalities = [
            "panoramic",
            "teleradiography",
            "intraoral-photo",
            "rawzip",
            "braintumor-mri-t1",
            "braintumor-mri-t2",
            "braintumor-mri-flair",
            "braintumor-mri-t1c",
        ]

        if modality_slug in no_processing_modalities:
            # Create completed job
            job_obj = Job.objects.create(
                modality_slug=modality_slug,
                **_entity_fk_kwargs(patient),
                input_file_path=key,
                status="completed",
                output_files={"input_format": file_format, "file_path": key},
            )
            job_obj.started_at = timezone.now()
            job_obj.completed_at = timezone.now()
            job_obj.save()
        else:
            # Create pending job for modalities that need processing
            job_obj = Job.objects.create(
                modality_slug=modality_slug,
                **_entity_fk_kwargs(patient),
                input_file_path=key,
                status="pending",
                output_files={"input_format": file_format, "expected_outputs": []},
            )
    except Exception as e:
        logger.error(f"Failed to create Job for {modality_slug}: {e}")

    return fr, job_obj


def save_generic_modality_folder(patient: Patient, modality_slug: str, folder_files):
    """Save a folder upload for an arbitrary modality slug and create a Job.
    Similar to save_cbct_folder_to_dataset but generic and sets FileRegistry.modality.
    """
    base_prefix = f"{_raw_key_prefix_for(patient, modality_slug)}/{modality_slug}_patient_{patient.patient_id}_folder"
    saved_files = []
    total_size = 0
    for f in folder_files:
        rel = _sanitize_relpath(getattr(f, "name", "file"))
        obj_key = f"{base_prefix}/{rel}" if rel else f"{base_prefix}/file"
        obj_key, file_size, file_hash = _upload_uploaded_file_to_storage(
            key=obj_key, uploaded_file=f
        )
        total_size += file_size
        saved_files.append(
            {
                "name": getattr(f, "name", "file"),
                "path": obj_key,
                "size": file_size,
                "hash": file_hash,
            }
        )

    combined_hashes = "".join(f.get("hash", "") for f in saved_files)
    hash_sha256 = hashlib.sha256()
    hash_sha256.update(combined_hashes.encode())
    folder_hash = hash_sha256.hexdigest()
    modality_fk = None
    try:
        from common.models import Modality as _Modality

        modality_fk = _Modality.objects.filter(slug=modality_slug).first()
    except Exception:
        modality_fk = None
    # Determine file_type for folder upload using centralized function
    folder_file_type = get_file_type_for_modality(modality_slug, is_processed=False)

    try:
        fr = FileRegistry.objects.create(
            file_type=folder_file_type,
            file_path=base_prefix,
            file_size=total_size,
            file_hash=folder_hash,
            **_entity_fk_kwargs(patient),
            modality=modality_fk,
            metadata={
                "uploaded_at": timezone.now().isoformat(),
                "input_type": "folder",
                "file_count": len(saved_files),
                "modality_slug": modality_slug,
                "files": saved_files,
            },
        )
    except Exception:
        logger.exception(
            "Failed to create FileRegistry (folder) for %s; proceeding to create Job anyway",
            modality_slug,
        )
        fr = None
    job = Job.objects.create(
        modality_slug=modality_slug,
        **_entity_fk_kwargs(patient),
        input_file_path=base_prefix,
        output_files={
            "input_type": "folder",
            "file_count": len(saved_files),
            "input_files": [f.get("path") for f in saved_files if isinstance(f, dict)],
            "expected_outputs": [],
        },
    )
    return fr, job


def save_cbct_to_dataset(patient_or_legacy, cbct_file):
    """
    Save CBCT file to object storage and create processing job
    Supports multiple formats: DICOM, NIfTI, MetaImage, NRRD

    Args:
        patient_or_legacy: Patient or legacy object with .patient
        cbct_file: Django UploadedFile instance

    Returns:
        tuple: (file_path, processing_job)
    """
    patient = _get_patient(patient_or_legacy)

    original_name = cbct_file.name
    filename_lower = original_name.lower()
    if filename_lower.endswith(".nii.gz"):
        extension = ".nii.gz"
        file_format = "nifti_compressed"
    elif filename_lower.endswith(".nii"):
        extension = ".nii"
        file_format = "nifti"
    elif filename_lower.endswith((".dcm", ".dicom")):
        extension = ".dcm"
        file_format = "dicom_single"
    elif filename_lower == "dicomdir" or filename_lower.endswith("/dicomdir"):
        extension = ""
        file_format = "dicomdir"
    elif filename_lower.endswith(".mha"):
        extension = ".mha"
        file_format = "metaimage"
    elif filename_lower.endswith(".mhd"):
        extension = ".mhd"
        file_format = "metaimage_header"
    elif filename_lower.endswith(".nrrd"):
        extension = ".nrrd"
        file_format = "nrrd"
    elif filename_lower.endswith(".nhdr"):
        extension = ".nhdr"
        file_format = "nrrd_header"
    elif filename_lower.endswith(".zip"):
        extension = ".zip"
        file_format = "dicom_archive_zip"
    elif filename_lower.endswith((".tar", ".tar.gz", ".tgz")):
        if filename_lower.endswith(".tar.gz"):
            extension = ".tar.gz"
        elif filename_lower.endswith(".tgz"):
            extension = ".tgz"
        else:
            extension = ".tar"
        file_format = "dicom_archive_tar"
    else:
        # Fallback - treat as raw DICOM
        extension = os.path.splitext(original_name)[1] or ".dcm"
        file_format = "unknown"

    # Generate filename preserving original extension
    base_filename = f"cbct_patient_{patient.patient_id}"
    if extension == "":  # Special case for DICOMDIR
        filename = f"{base_filename}_DICOMDIR"
    else:
        filename = f"{base_filename}{extension}"

    # Clean up existing CBCT files and registry entries for this patient
    cbct_raw_type = get_file_type_for_modality("cbct", is_processed=False)
    cbct_processed_type = get_file_type_for_modality("cbct", is_processed=True)
    existing_raw_files = FileRegistry.objects.filter(
        file_type=cbct_raw_type, **_entity_filter_kwargs(patient)
    )

    # Also clean up any existing processed CBCT files
    existing_processed_files = FileRegistry.objects.filter(
        file_type=cbct_processed_type, **_entity_filter_kwargs(patient)
    )

    key = f"{_raw_key_prefix_for(patient, 'cbct')}/{filename}"
    key, file_size, file_hash = _upload_uploaded_file_to_storage(
        key=key, uploaded_file=cbct_file
    )
    modality_fk = None
    try:
        from common.models import Modality as _Modality

        modality_fk = _Modality.objects.filter(slug="cbct").first()
    except Exception:
        modality_fk = None
    # Create file registry entry with format metadata
    file_registry = FileRegistry.objects.create(
        file_type=get_file_type_for_modality("cbct", is_processed=False),
        file_path=key,
        file_size=file_size,
        file_hash=file_hash,
        **_entity_fk_kwargs(patient),
        modality=modality_fk,
        metadata={
            "original_filename": original_name,
            "uploaded_at": timezone.now().isoformat(),
            "file_format": file_format,
            "needs_conversion": file_format != "nifti_compressed",
        },
    )

    # Create job
    processing_job = Job.objects.create(
        modality_slug="cbct",
        **_entity_fk_kwargs(patient),
        input_file_path=key,
        output_files={
            "input_format": file_format,
            "expected_outputs": [
                "volume_nifti",
                "panoramic_view",
                "structures_mesh",
            ],
        },
    )

    return key, processing_job


def save_cbct_folder_to_dataset(patient_or_legacy, folder_files):
    """
    Save CBCT folder (multiple DICOM files) to object storage and create processing job

    Args:
        patient_or_legacy: Patient or legacy object with .patient
        folder_files: List of Django UploadedFile instances from folder

    Returns:
        tuple: (folder_path, processing_job)
    """
    from .models import validate_cbct_folder

    patient = _get_patient(patient_or_legacy)

    # Validate folder contents
    valid_files = validate_cbct_folder(folder_files)

    base_prefix = f"{_raw_key_prefix_for(patient, 'cbct')}/cbct_patient_{patient.patient_id}_folder"

    # Clean up existing CBCT files and registry entries for this patient
    cbct_raw_type = get_file_type_for_modality("cbct", is_processed=False)
    existing_raw_files = FileRegistry.objects.filter(
        file_type=cbct_raw_type, **_entity_filter_kwargs(patient)
    )

    # Save all valid files to object storage
    saved_files = []
    total_size = 0

    for file in valid_files:
        rel = _sanitize_relpath(getattr(file, "name", "file"))
        obj_key = f"{base_prefix}/{rel}" if rel else f"{base_prefix}/file"
        obj_key, file_size, file_hash = _upload_uploaded_file_to_storage(
            key=obj_key, uploaded_file=file
        )
        total_size += file_size

        saved_files.append(
            {"name": file.name, "path": obj_key, "size": file_size, "hash": file_hash}
        )

    # Calculate folder hash (hash of all file hashes combined)
    combined_hashes = "".join(f["hash"] for f in saved_files)
    hash_sha256 = hashlib.sha256()
    hash_sha256.update(combined_hashes.encode())
    folder_hash = hash_sha256.hexdigest()

    # Create file registry entry for the folder
    file_registry = FileRegistry.objects.create(
        file_type=get_file_type_for_modality("cbct", is_processed=False),
        file_path=base_prefix,
        file_size=total_size,
        file_hash=folder_hash,
        **_entity_fk_kwargs(patient),
        metadata={
            "upload_type": "folder",
            "file_format": "dicom_folder",
            "uploaded_at": timezone.now().isoformat(),
            "files": saved_files,  # List of all files in folder
            "needs_conversion": True,
        },
    )

    # Create job
    processing_job = Job.objects.create(
        modality_slug="cbct",
        **_entity_fk_kwargs(patient),
        input_file_path=base_prefix,
        output_files={
            "input_format": "dicom_folder",
            "input_type": "folder",
            "file_count": len(saved_files),
            "input_files": [f.get("path") for f in saved_files if isinstance(f, dict)],
            "expected_outputs": [
                "volume_nifti",
                "panoramic_view",
                "structures_mesh",
            ],
        },
    )

    return base_prefix, processing_job


def save_ios_to_dataset(patient_or_legacy, upper_file=None, lower_file=None):
    """
    Save IOS files to object storage and create processing job

    Args:
        patient_or_legacy: Patient or legacy object with .patient
        upper_file: Django UploadedFile instance for upper scan
        lower_file: Django UploadedFile instance for lower scan

    Returns:
        dict: {'files': [...], 'processing_job': job}
    """
    patient = _get_patient(patient_or_legacy)

    saved_files = []
    file_registries = []

    # Save upper scan if provided
    if upper_file:
        filename = f"ios_upper_patient_{patient.patient_id}.stl"
        key = f"{_raw_key_prefix_for(patient, 'ios')}/{filename}"
        key, file_size, file_hash = _upload_uploaded_file_to_storage(
            key=key, uploaded_file=upper_file
        )
        modality_fk = None
        try:
            from common.models import Modality as _Modality

            modality_fk = _Modality.objects.filter(slug="ios").first()
        except Exception:
            modality_fk = None
        file_registry = FileRegistry.objects.create(
            file_type=get_file_type_for_modality(
                "ios", is_processed=False, subtype="upper"
            ),
            file_path=key,
            file_size=file_size,
            file_hash=file_hash,
            **_entity_fk_kwargs(patient),
            modality=modality_fk,
            metadata={
                "original_filename": upper_file.name,
                "uploaded_at": timezone.now().isoformat(),
            },
        )

        saved_files.append(("upper", key))
        file_registries.append(file_registry)

    # Save lower scan if provided
    if lower_file:
        filename = f"ios_lower_patient_{patient.patient_id}.stl"
        key = f"{_raw_key_prefix_for(patient, 'ios')}/{filename}"
        key, file_size, file_hash = _upload_uploaded_file_to_storage(
            key=key, uploaded_file=lower_file
        )

        file_registry = FileRegistry.objects.create(
            file_type=get_file_type_for_modality(
                "ios", is_processed=False, subtype="lower"
            ),
            file_path=key,
            file_size=file_size,
            file_hash=file_hash,
            **_entity_fk_kwargs(patient),
            metadata={
                "original_filename": lower_file.name,
                "uploaded_at": timezone.now().isoformat(),
            },
        )

        saved_files.append(("lower", key))
        file_registries.append(file_registry)

    # Create processing job if we have files
    processing_job = None
    bite_classification_job = None
    if saved_files:
        input_files = {scan_type: path for scan_type, path in saved_files}

        processing_job = Job.objects.create(
            modality_slug="ios",
            **_entity_fk_kwargs(patient),
            input_file_path=json.dumps(input_files),
        )

        # Always create a fresh stage-2 bite job for every new IOS upload.
        # Reusing completed jobs can leave status transitions stale and skip execution.
        bite_classification_job = Job.objects.create(
            modality_slug="bite_classification",
            status="dependency",
            **_entity_fk_kwargs(patient),
            input_file_path=f"Waiting for IOS Job #{processing_job.id} to complete",
            priority=processing_job.priority,
            output_files={
                "expected_outputs": ["*_bite_classification_results.json"],
                "depends_on_ios_job": processing_job.id,
                "ios_job_id": processing_job.id,
            },
        )
        bite_classification_job.add_dependency(processing_job)

        logger.info(
            f"Created bite classification job #{bite_classification_job.id} with dependency on IOS job #{processing_job.id}"
        )

    return {
        "files": saved_files,
        "file_registries": file_registries,
        "processing_job": processing_job,
        "bite_classification_job": bite_classification_job,
    }


def save_audio_to_dataset(voice_caption, audio_file):
    """
    Save audio file to object storage and create processing job

    Args:
        voice_caption: VoiceCaption instance
        audio_file: Django UploadedFile instance

    Returns:
        tuple: (file_path, processing_job)
    """
    patient = _get_patient(voice_caption)

    # Generate filename: audio_voice_{id}_patient_{patient_id}.webm
    original_name = audio_file.name
    extension = Path(original_name).suffix or ".webm"
    filename = f"audio_voice_{voice_caption.id}_patient_{patient.patient_id}{extension}"
    key = f"{_raw_key_prefix_for(patient, 'audio')}/{filename}"
    key, file_size, file_hash = _upload_uploaded_file_to_storage(
        key=key, uploaded_file=audio_file
    )

    # Create file registry entry
    file_registry = FileRegistry.objects.create(
        file_type=get_file_type_for_modality("audio", is_processed=False),
        file_path=key,
        file_size=file_size,
        file_hash=file_hash,
        **_voice_entity_fk_kwargs(voice_caption),
        **_entity_fk_kwargs(patient),
        metadata={
            "original_filename": original_name,
            "duration": voice_caption.duration,
            "modality": voice_caption.modality,
            "uploaded_at": timezone.now().isoformat(),
        },
    )

    # Create processing job
    processing_job = Job.objects.create(
        modality_slug="audio",
        **_voice_entity_fk_kwargs(voice_caption),
        **_entity_fk_kwargs(patient),
        input_file_path=key,
    )

    return key, processing_job


def save_rgb_images_to_dataset(patient_or_legacy, images):
    """Save one or more RGB images for a patient to object storage and register them.

    Args:
        patient_or_legacy: Patient or legacy object with .patient
        images: iterable of UploadedFile

    Returns:
        tuple(list[FileRegistry], list[dict]): (saved_entries, errors)
    """
    patient = _get_patient(patient_or_legacy)

    saved_entries = []
    errors = []

    for idx, img in enumerate(images):
        try:
            original_name = img.name
            name_lower = original_name.lower()
            # Accept common RGB formats
            valid_exts = [".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"]
            ext = Path(original_name).suffix.lower()
            if ext not in valid_exts:
                # Try to infer via content-type if no/unknown extension
                ext = ext if ext else ".png"

            # Optionally parse a friendly label from field name; support (name,img) tuples
            label = getattr(img, "label", "") or ""

            filename = f"rgb_{patient.patient_id}_{timezone.now().strftime('%Y%m%d_%H%M%S')}_{idx}{ext}"
            key = f"{_raw_key_prefix_for(patient, 'rgb')}/{filename}"
            key, file_size, file_hash = _upload_uploaded_file_to_storage(
                key=key, uploaded_file=img
            )

            entry = FileRegistry.objects.create(
                file_type=get_file_type_for_modality(
                    "rgb", is_processed=False, file_format=ext
                ),
                file_path=key,
                file_size=file_size,
                file_hash=file_hash,
                **_entity_fk_kwargs(patient),
                metadata={
                    "original_filename": original_name,
                    "label": label,
                    "uploaded_at": timezone.now().isoformat(),
                },
            )
            saved_entries.append(entry)
        except Exception as e:
            logger.error(f"Error saving RGB image {getattr(img, 'name', '')}: {e}")
            errors.append({"name": getattr(img, "name", ""), "error": str(e)})

    return saved_entries, errors


def save_intraoral_photos_to_dataset(patient_or_legacy, images):
    """Save multiple intraoral images for a patient and create FileRegistry entries.
    Returns (saved_entries, errors, job) where saved_entries is a list of FileRegistry objects,
    errors is a list of error messages for failed uploads, and job is the processing job.
    """
    patient = _get_patient(patient_or_legacy)

    saved_entries = []
    errors = []
    saved_files = []
    summary_key = f"{_raw_key_prefix_for(patient, 'intraoral')}/intraoral_patient_{patient.patient_id}_summary.txt"

    # Resolve modality FK for FileRegistry
    modality_fk = None
    try:
        from common.models import Modality as _Modality

        modality_fk = _Modality.objects.filter(slug="intraoral-photo").first()
    except Exception:
        pass

    for idx, img in enumerate(images):
        try:
            original_name = getattr(img, "name", f"intraoral_{idx}.jpg")
            ext = os.path.splitext(original_name)[1].lower() or ".jpg"

            filename = f"intraoral_{idx + 1}_patient_{patient.patient_id}{ext}"
            key = f"{_raw_key_prefix_for(patient, 'intraoral')}/{filename}"
            key, file_size, file_hash = _upload_uploaded_file_to_storage(
                key=key, uploaded_file=img
            )

            entry = FileRegistry.objects.create(
                file_type="intraoral_raw",  # Use legacy file_type for FileRegistry
                file_path=key,
                file_size=file_size,
                file_hash=file_hash,
                **_entity_fk_kwargs(patient),
                modality=modality_fk,
                metadata={
                    "original_filename": original_name,
                    "image_index": idx + 1,
                    "uploaded_at": timezone.now().isoformat(),
                },
            )
            saved_entries.append(entry)
            saved_files.append(key)
        except Exception as e:
            logger.error(f"Error saving intraoral image {idx}: {e}", exc_info=True)
            errors.append(f"Failed to save image {idx + 1}: {str(e)}")

    # Create completed job (intraoral photos don't need processing)
    job = None
    if saved_files:
        try:
            # Store input manifest directly as JSON in Job for runner consumption

            job = Job.objects.create(
                modality_slug="intraoral-photo",
                **_entity_fk_kwargs(patient),
                input_file_path=json.dumps({"files": saved_files}),
                status="completed",
                output_files={
                    "input_type": "multiple_images",
                    "file_count": len(saved_files),
                    "files": saved_files,
                },
            )
            job.started_at = timezone.now()
            job.completed_at = timezone.now()
            job.save()
        except Exception as e:
            logger.error(f"Error creating intraoral job: {e}", exc_info=True)

    return saved_entries, errors, job


def mark_job_completed(job_id, output_files, logs=None):
    """
    Mark a processing job as completed and register output files.
    This is what the external Docker containers will call.

    Args:
        job_id: ProcessingJob ID
        output_files: dict of output file paths
        logs: optional processing logs
    """
    logger.info(
        f"mark_job_completed called with job_id={job_id}, output_files={output_files}, logs present={logs is not None}"
    )

    try:
        job = Job.objects.select_related(
            "patient", "brain_patient", "voice_caption", "brain_voice_caption"
        ).get(id=job_id)
        logger.info(
            f"Found job: {job.id}, modality: {job.modality_slug}, status: {job.status}"
        )
        job_patient = _job_patient(job)
        job_voice_caption = _job_voice_caption(job)

        # For IOS -> bite stage chaining, update dependent job inputs before
        # marking IOS as completed. This avoids enqueueing bite jobs with stale
        # placeholder input paths when dependency status flips to pending.
        if job.modality_slug == "ios" and output_files:
            try:
                dependent_bite_jobs = job.dependent_jobs.filter(
                    modality_slug="bite_classification"
                )
                for bite_job in dependent_bite_jobs:
                    bite_job.input_file_path = json.dumps(output_files)
                    bite_job.save(update_fields=["input_file_path"])
                    logger.info(
                        f"Pre-updated bite classification job #{bite_job.id} with IOS output files: {list(output_files.keys())}"
                    )
            except Exception as e:
                logger.error(
                    f"Error pre-updating dependent bite classification jobs: {e}"
                )

        job.mark_completed(output_files)
        logger.info(f"Job marked as completed successfully")

        # Register output files
        logger.info(f"Registering output files for modality: {job.modality_slug}")

        if job.modality_slug == "cbct":
            # For CBCT, we expect multiple output files
            # output_files should contain: pano, volume_nifti, structures_mesh_*, etc.
            processed_files = {}
            total_size = 0

            # Clean up any existing processed CBCT files for this patient
            cbct_processed_type = get_file_type_for_modality("cbct", is_processed=True)
            existing_processed_files = FileRegistry.objects.filter(
                file_type=cbct_processed_type, **_job_entity_fk_kwargs(job)
            )
            # Remove existing DB entries only; keep object storage files
            try:
                existing_count = existing_processed_files.count()
                if existing_count:
                    logger.info(
                        f"Deleting {existing_count} existing {cbct_processed_type} FileRegistry entries for patient {getattr(job_patient, 'patient_id', 'unknown')}"
                    )
                    existing_processed_files.delete()
            except Exception as e:
                logger.error(
                    f"Error deleting existing {cbct_processed_type} FileRegistry entries: {e}"
                )

            for file_type, out_spec in output_files.items():
                path_or_key = _resolve_output_path_or_key(out_spec)
                logger.info(
                    f"Processing CBCT output: type={file_type}, path_or_key={path_or_key}"
                )
                if not path_or_key or not artifact_exists(path_or_key):
                    logger.warning(f"Output not found: {path_or_key}")
                    continue

                file_size, file_hash = _size_hash_for_path_or_key(path_or_key)
                if isinstance(file_size, int):
                    total_size += file_size

                processed_files[file_type] = {
                    "path": path_or_key,
                    "size": file_size,
                    "hash": file_hash,
                    "type": file_type,
                }

            # Create single FileRegistry entry for CBCT with all outputs in metadata
            if processed_files:
                # Use pano path as primary file path (for backward compatibility)
                primary_path = processed_files.get("panoramic_view", {}).get("path", "")
                if not primary_path and processed_files:
                    # Fallback to first available file
                    primary_path = list(processed_files.values())[0]["path"]

                FileRegistry.objects.create(
                    file_type=get_file_type_for_modality("cbct", is_processed=True),
                    file_path=primary_path,  # Primary file path (e.g., pano)
                    file_size=total_size,  # Total size of all files
                    file_hash="multi-file",  # Indicator that this contains multiple files
                    processing_job=job,
                    **_job_entity_fk_kwargs(job),
                    metadata={
                        "processed_at": timezone.now().isoformat(),
                        "files": processed_files,  # All output files stored here
                        "logs": logs if logs else "",
                    },
                )
                logger.info(
                    f"CBCT FileRegistry entry created with {len(processed_files)} output files"
                )

        else:
            # For non-CBCT modalities, register simple outputs idempotently.
            # Bite classification has a dedicated handler below.
            if job.modality_slug != "bite_classification":
                for file_type, out_spec in output_files.items():
                    path_or_key = _resolve_output_path_or_key(out_spec)
                    logger.info(
                        f"Processing output file: type={file_type}, path_or_key={path_or_key}"
                    )
                    if not path_or_key or not artifact_exists(path_or_key):
                        continue

                    file_size, file_hash = _size_hash_for_path_or_key(path_or_key)

                    if job.modality_slug == "ios":
                        registry_type = f"ios_processed_{file_type}"
                    else:
                        registry_type = get_file_type_for_modality(
                            job.modality_slug, is_processed=True
                        )
                    logger.info(f"Storing FileRegistry entry with type={registry_type}")

                    FileRegistry.objects.update_or_create(
                        file_path=path_or_key,
                        defaults={
                            "file_type": registry_type,
                            "file_size": file_size or 0,
                            "file_hash": file_hash or "object",
                            "processing_job": job,
                            **_job_entity_fk_kwargs(job),
                            "metadata": {
                                "processed_at": timezone.now().isoformat(),
                                "logs": logs if logs else "",
                            },
                        },
                    )
                    logger.info("FileRegistry entry stored/updated successfully")

        # Update related model status
        logger.info(f"Updating related model status for modality: {job.modality_slug}")
        if job_patient and job.modality_slug == "cbct":
            logger.info(f"Updating patient CBCT processing status")
            job_patient.cbct_processing_status = "processed"
            job_patient.save()
        elif job_patient and job.modality_slug == "ios":
            logger.info(f"Updating patient IOS processing status")
            job_patient.ios_processing_status = "processed"
            job_patient.save()
        elif job_voice_caption and job.modality_slug == "audio":
            job_voice_caption.processing_status = "completed"

            # Use logs parameter directly if it contains transcription text
            if logs and isinstance(logs, str) and logs.strip():
                job_voice_caption.text_caption = logs.strip()
                logger.info(
                    f"Successfully saved transcription from logs: {logs[:50]}..."
                )
            else:
                logger.warning(f"Logs parameter is empty or invalid: {logs}")
                # Fallback: try to extract text from output files if available
                text_extracted = False
                for out_spec in output_files.values():
                    path_or_key = _resolve_output_path_or_key(out_spec)
                    if not path_or_key or not str(path_or_key).endswith(".txt"):
                        continue
                    try:
                        fh, _ = open_binary(path_or_key)
                        try:
                            text_content = (
                                fh.read().decode("utf-8", errors="replace").strip()
                            )
                        finally:
                            with contextlib.suppress(Exception):
                                fh.close()
                        if text_content:
                            job_voice_caption.text_caption = text_content
                            text_extracted = True
                            logger.info(
                                f"Successfully extracted text from {path_or_key}: {text_content[:50]}..."
                            )
                        else:
                            logger.warning(f"Text file {path_or_key} is empty")
                    except Exception as e:
                        logger.error(f"Error reading text file {path_or_key}: {e}")

                if not text_extracted:
                    logger.warning(
                        f"No text was extracted for voice caption {job_voice_caption.id}"
                    )
                    # Set a placeholder text to indicate processing completed but no text found
                    job_voice_caption.text_caption = ""

            # Save the original transcription when processing is first completed
            job_voice_caption.save_original_transcription()
            job_voice_caption.save()

        elif job_patient and job.modality_slug == "bite_classification":
            logger.info(
                f"Bite classification job completed for patient {getattr(job_patient, 'patient_id', 'unknown')}"
            )

            try:
                classification_file = None
                for file_type, out_spec in output_files.items():
                    path_or_key = _resolve_output_path_or_key(out_spec)
                    if (
                        str(path_or_key).endswith("_bite_classification_results.json")
                        or "bite_classification" in file_type.lower()
                        or "classification" in file_type.lower()
                    ):
                        classification_file = path_or_key
                        break

                if classification_file and artifact_exists(classification_file):
                    logger.info(f"Found classification file: {classification_file}")

                    fh, _ = open_binary(classification_file)
                    try:
                        classification_data = json.loads(
                            fh.read().decode("utf-8", errors="replace")
                        )
                    finally:
                        with contextlib.suppress(Exception):
                            fh.close()

                    sagittal_left = classification_data.get("sagittal_left", "Unknown")
                    sagittal_right = classification_data.get(
                        "sagittal_right", "Unknown"
                    )
                    vertical = classification_data.get("vertical", "Unknown")
                    transverse = classification_data.get("transverse", "Unknown")
                    midline = classification_data.get("midline", "Unknown")

                    if any(
                        val != "Unknown"
                        for val in [
                            sagittal_left,
                            sagittal_right,
                            vertical,
                            transverse,
                            midline,
                        ]
                    ):
                        # Keep classification-specific writes in a savepoint so
                        # optional metadata failures do not poison the outer
                        # completion transaction.
                        with transaction.atomic():
                            classification, created = (
                                Classification.objects.get_or_create(
                                    patient=job_patient,
                                    classifier="pipeline",
                                    defaults={
                                        "sagittal_left": sagittal_left,
                                        "sagittal_right": sagittal_right,
                                        "vertical": vertical,
                                        "transverse": transverse,
                                        "midline": midline,
                                        "annotator": None,
                                    },
                                )
                            )

                            if not created:
                                classification.sagittal_left = sagittal_left
                                classification.sagittal_right = sagittal_right
                                classification.vertical = vertical
                                classification.transverse = transverse
                                classification.midline = midline
                                classification.save()

                            logger.info(
                                f"{'Created' if created else 'Updated'} classification for patient {getattr(job_patient, 'patient_id', 'unknown')}"
                            )

                            file_size, file_hash = _size_hash_for_path_or_key(
                                classification_file
                            )

                            FileRegistry.objects.update_or_create(
                                file_path=classification_file,
                                defaults={
                                    "file_type": get_file_type_for_modality(
                                        "bite_classification", is_processed=True
                                    ),
                                    "file_size": file_size or 0,
                                    "file_hash": file_hash or "object",
                                    "processing_job": job,
                                    **_job_entity_fk_kwargs(job),
                                    "metadata": {
                                        "processed_at": timezone.now().isoformat(),
                                        "classification_results": classification_data,
                                        "logs": logs if logs else "",
                                    },
                                },
                            )

                            logger.info(
                                "Stored/updated classification file in FileRegistry"
                            )
                    else:
                        logger.warning(
                            f"Classification file contains no valid classification data: {classification_data}"
                        )

                else:
                    logger.warning(
                        f"No classification file found in output files: {output_files}"
                    )

            except Exception as e:
                logger.error(
                    f"Error processing bite classification completion for patient {getattr(job_patient, 'patient_id', 'unknown')}: {e}"
                )
                logger.error(f"Full traceback: {traceback.format_exc()}")

        logger.info(f"mark_job_completed completed successfully")
        return True

    except Job.DoesNotExist:
        logger.error(f"Job with ID {job_id} does not exist")
        return False
    except Exception as e:
        logger.error(f"Error in mark_job_completed for job_id={job_id}: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        raise


def mark_job_failed(job_id, error_msg, can_retry=True):
    """
    Mark a processing job as failed.
    This is what the external Docker containers will call.

    Args:
        job_id: ProcessingJob ID
        error_msg: Error message
        can_retry: Whether the job can be retried
    """
    try:
        job = Job.objects.select_related(
            "patient", "brain_patient", "voice_caption", "brain_voice_caption"
        ).get(id=job_id)
        job_patient = _job_patient(job)
        job_voice_caption = _job_voice_caption(job)
        job.mark_failed(error_msg, can_retry)

        if job_patient and job.modality_slug == "cbct":
            job_patient.cbct_processing_status = "failed"
            job_patient.save()
        elif job_patient and job.modality_slug == "ios":
            job_patient.ios_processing_status = "failed"
            job_patient.save()
        elif job_voice_caption and job.modality_slug == "audio":
            job_voice_caption.processing_status = "failed"
            job_voice_caption.save()
        elif job_patient and job.modality_slug == "bite_classification":
            logger.info(
                f"Bite classification job failed for patient {getattr(job_patient, 'patient_id', 'unknown')}"
            )

        return True

    except Job.DoesNotExist:
        return False
