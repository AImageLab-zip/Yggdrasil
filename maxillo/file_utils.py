import contextlib
import hashlib
import json
import logging
import os
import tarfile
import traceback
import zipfile
from pathlib import Path

from common.job_routing import is_runner_enabled_for_modality
from common.modality_config import get_step, modality_requires_processing
from common.models import FileRegistry, Job
from common.uploads import create_step_jobs
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Classification, Patient, VoiceCaption

logger = logging.getLogger(__name__)

# ProcessingStep slug of the bite-classification stage, and so the value of
# Job.modality_slug for its jobs. Distinct from the "bite_classification"
# *file type* that get_file_type_for_modality returns for its outputs.
BITE_CLASSIFICATION_SLUG = "ios-bite-classification"

DEFAULT_PANORAMIC_OUTPUT = "panoramic_png"
PANORAMIC_OUTPUT_KEYS = {
    "panoramic_zplus40_mean_png",
    "panoramic_zplus40_raysum_png",
    "panoramic_zplus20_mean_png",
    "panoramic_zplus20_raysum_png",
    DEFAULT_PANORAMIC_OUTPUT,
    "panoramic_z0_raysum_png",
    "panoramic_zminus20_mean_png",
    "panoramic_zminus20_raysum_png",
    "panoramic_zminus40_mean_png",
    "panoramic_zminus40_raysum_png",
}

from common.file_access import exists as artifact_exists
from common.file_access import open_binary
from common.object_storage import get_object_storage
from common.uploads import (
    domain_for_patient as _domain_for_patient,
    entity_fk_kwargs as _entity_fk_kwargs,
    get_patient as _get_patient,
    processed_key_prefix_for as _processed_key_prefix_for,
    project_slug_from_patient as _project_slug_from_patient,
    raw_key_prefix_for as _raw_key_prefix_for,
    sanitize_relpath as _sanitize_relpath,
    upload_uploaded_file_to_storage as _upload_uploaded_file_to_storage,
)


def _create_job_if_runner_enabled(modality_slug, **kwargs):
    if not is_runner_enabled_for_modality(modality_slug):
        logger.info(
            "Not creating Job for disabled modality '%s'", modality_slug
        )
        return None
    job = Job.objects.create(modality_slug=modality_slug, **kwargs)
    # A pending source job triggers its modality's downstream step pipeline
    # (and any cross-modality dependents, e.g. ios -> bite_classification).
    # No-op when the modality declares no ProcessingStep pipeline.
    if job.status == "pending":
        create_step_jobs(job)
    return job


def _intraoral_images_by_reference(job, references):
    """This job's intraoral photographs, indexed by every name an algorithm may use.

    **Two names, because an algorithm on the cluster only knows one of them.** The
    contract was written around ``FileRegistry`` ids, and the ids reach the cluster
    only as *keys* of ``job.input_files`` -- what a ``run.sbatch`` is handed is
    ``YGG_INPUT_KEYS``, a list of object-storage keys, and ``ygg-stage pull`` writes
    each one under its basename. So an algorithm can name the image it segmented by
    its storage key and cannot name it by id without the runner inventing a channel
    to carry the mapping.

    ``FileRegistry.file_path`` *is* the object-storage key, so accepting both costs a
    second index over the same rows and no new plumbing. Ids stay valid: the existing
    contract and everything written against it are unchanged.

    Only ``intraoral_raw``/``intraoral_processed`` rows belonging to this job's patient
    are indexed, so a reference to somebody else's image resolves to nothing rather
    than to their image.
    """
    references = [str(reference) for reference in references]
    ids = [int(ref) for ref in references if ref.lstrip("-").isdigit()]
    rows = FileRegistry.objects.filter(
        file_type__in=["intraoral_raw", "intraoral_processed"],
        **_job_entity_fk_kwargs(job),
    ).filter(Q(id__in=ids) | Q(file_path__in=references))

    by_reference = {}
    for row in rows:
        by_reference[str(row.id)] = row
        by_reference[row.file_path] = row
    return by_reference


#: The five standard intraoral views the classifier assigns, as it spells them.
INTRAORAL_VIEWS = frozenset(
    {"frontal", "left_buccal", "right_buccal", "upper_occlusal", "lower_occlusal"}
)


def _apply_intraoral_views(job, views_output):
    """Record each photograph's clinical view on its own registry row.

    IOP-Compass classifies the view *before* it segments, because the view selects the
    SegmentAnyTooth detector -- so the label is produced whether or not anyone asks for
    it, and it is the one fact about an intraoral photograph nothing else in Yggdrasil
    records. Stored in ``FileRegistry.subtype``, which is already the column for "which
    flavour of this file type this row is" and is blank on every ``intraoral_raw`` row;
    the ``intraoral-photo.raw`` export artifact places no constraint on it, so nothing
    downstream changes shape.

    Unknown labels are ignored rather than written: ``subtype`` is shown to readers, and
    a misspelling from a future model version would render as a view that does not
    exist. Returns how many rows were labelled.
    """
    views_key = _resolve_output_path_or_key(views_output)
    if not views_key:
        return 0
    try:
        fh, _ = open_binary(views_key)
        try:
            views = json.loads(fh.read().decode("utf-8", errors="replace"))
        finally:
            with contextlib.suppress(Exception):
                fh.close()
    except Exception:
        logger.exception("Could not read the intraoral view labels for job %s", job.id)
        return 0

    if not isinstance(views, dict):
        return 0

    rows = _intraoral_images_by_reference(job, views.keys())
    labelled = 0
    for reference, view in views.items():
        row = rows.get(str(reference))
        if row is None or str(view) not in INTRAORAL_VIEWS:
            continue
        if row.subtype != str(view):
            row.subtype = str(view)
            row.save(update_fields=["subtype"])
        labelled += 1
    return labelled


#: Bits2Bites task name -> (Classification field, class labels in the model's own
#: index order). The model's vocabularies live in its
#: ``pointcept/datasets/dental.py`` (kept in Italian to match the annotation CSVs);
#: these are the same lists in the same order, spelled as Classification's choices.
#: Note ``transverse`` deliberately reads normal/scissor/cross -- the model's order --
#: which is *not* the order of Classification.TRANSVERSE_CHOICES.
BITE_TASK_FIELDS = {
    "right_occ": ("sagittal_right", ["I", "II", "III"]),
    "left_occ": ("sagittal_left", ["I", "II", "III"]),
    "anterior_bite": ("vertical", ["normal", "deep", "open", "reverse"]),
    "transverse_bite": ("transverse", ["normal", "scissor", "cross"]),
    "midline": ("midline", ["centered", "deviated"]),
}


def bite_classification_values(predictions, patient_id):
    """Map Bits2Bites ``predictions.json`` onto Classification field values.

    ``predictions`` is the parsed file: a list with one object per patient, keyed
    ``name`` = ``dental_<zero-padded id>`` (the id ``prepare_yggdrasil_input``
    wrote), each task giving ``{"pred": <class index>, "prob": [...], "gt": -1}``.
    ``gt`` is always -1 here: Yggdrasil supplies placeholder labels for pure
    inference, so the run's own accuracy metrics are meaningless and only ``pred``
    is read.

    Returns a dict of the five Classification fields, each ``"Unknown"`` when the
    task is absent or its index is out of vocabulary. Returns ``None`` -- writing
    nothing rather than another patient's result -- when the file does not carry
    exactly one entry, or that entry names a different patient.
    """
    if not isinstance(predictions, list) or len(predictions) != 1:
        logger.error(
            "Bite predictions for patient %s must hold exactly one entry, got %s",
            patient_id,
            len(predictions) if isinstance(predictions, list) else type(predictions),
        )
        return None

    entry = predictions[0]
    if not isinstance(entry, dict):
        logger.error("Bite predictions entry for patient %s is not an object", patient_id)
        return None

    expected = f"dental_{int(patient_id):04d}"
    if str(entry.get("name")) != expected:
        logger.error(
            "Bite predictions name %r does not identify patient %s (expected %r)",
            entry.get("name"),
            patient_id,
            expected,
        )
        return None

    values = {}
    for task, (field, labels) in BITE_TASK_FIELDS.items():
        task_result = entry.get(task)
        index = task_result.get("pred") if isinstance(task_result, dict) else None
        if isinstance(index, int) and 0 <= index < len(labels):
            values[field] = labels[index]
        else:
            logger.warning(
                "Bite task %r for patient %s has no usable prediction (%r)",
                task,
                patient_id,
                index,
            )
            values[field] = "Unknown"
    return values


def _landmark_output(output_files):
    for output_name, output_spec in (output_files or {}).items():
        if os.path.basename(str(output_name)).lower() in {"landmarks", "landmarks.json"}:
            return output_name, output_spec
    return None, None


def _ios_output_arch(output_name):
    name = os.path.splitext(os.path.basename(str(output_name)).lower())[0]
    tokens = name.replace("-", "_").split("_")
    if "upper" in tokens:
        return "upper"
    if "lower" in tokens:
        return "lower"
    return None


def get_file_type_for_modality(
    modality_slug, is_processed=False, file_format=None, subtype=None
):
    """
    Centralized function to determine the correct file_type for a given modality.

    Args:
        modality_slug: The modality slug (e.g., 'cbct', 'ios')
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
        file_type = f"ios_processed_{subtype}" if is_processed else f"ios_raw_{subtype}"
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

    # Fallback mappings for special cases. `ios` deliberately has no entry here:
    # every real caller either passes `subtype` (handled above) or wants the
    # generic `ios_raw`/`ios_processed` from the final fallback below (used by
    # mark_job_completed's generic registration branch) -- there is no code path
    # that calls this with modality_slug="ios" and no subtype expecting the
    # upper-arch default.
    fallback_mappings = {
        "cbct": "cbct_raw" if not is_processed else "cbct_processed",
        "audio": "audio_raw" if not is_processed else "audio_processed",
        "bite_classification": "bite_classification",  # Special case - no raw/processed distinction
        "intraoral-photo": "intraoral_raw"
        if not is_processed
        else "intraoral-photo_processed",
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
        if file_format in ["nii", "nii.gz", "mha", "mhd", "nrrd"]:
            return "volume_raw"
        elif file_format in ["jpg", "jpeg", "png", "bmp", "tiff", "tif"]:
            return "image_raw"

    # Final fallback: `choices=` on FileRegistry.file_type is advisory only (not
    # DB-enforced), so a modality/algorithm with no FILE_TYPE_CHOICES entry still
    # gets its own distinct, correctly namespaced file_type here instead of being
    # dumped into the generic_raw/generic_processed bucket. This is what lets a
    # freshly admin-declared ProcessingStep.slug (e.g. a new algorithm) register
    # its outputs with zero code changes.
    return potential_file_type


def _entity_filter_kwargs(patient):

    domain = _domain_for_patient(patient)
    if domain == "laparoscopy":
        return {
            "domain": "laparoscopy",
            "laparoscopy_patient": patient,
        }
    return {
        "domain": "maxillo",
        "patient": patient,
    }


def _voice_entity_fk_kwargs(voice_caption):

    patient = _get_patient(voice_caption)
    domain = _domain_for_patient(patient)
    if domain == "laparoscopy":
        return {
            "laparoscopy_voice_caption": voice_caption,
            "voice_caption": None,
            "brain_voice_caption": None,
        }
    return {
        "voice_caption": voice_caption,
        "brain_voice_caption": None,
        "laparoscopy_voice_caption": None,
    }


def _domain_for_job(job) -> str:

    if getattr(job, "domain", None) in ["maxillo", "laparoscopy"]:
        return job.domain
    if getattr(job, "laparoscopy_patient_id", None) or getattr(
        job, "laparoscopy_voice_caption_id", None
    ):
        return "laparoscopy"

    return "maxillo"


def _job_patient(job):

    domain = _domain_for_job(job)
    if domain == "laparoscopy":
        return getattr(job, "laparoscopy_patient", None)
    return getattr(job, "patient", None)


def _job_voice_caption(job):

    domain = _domain_for_job(job)
    if domain == "laparoscopy":
        return getattr(job, "laparoscopy_voice_caption", None)
    return getattr(job, "voice_caption", None)


def _job_entity_fk_kwargs(job):
    domain = _domain_for_job(job)
    if domain == "laparoscopy":
        return {
            "domain": "laparoscopy",
            "laparoscopy_patient": _job_patient(job),
            "patient": None,
            "brain_patient": None,
            "laparoscopy_voice_caption": _job_voice_caption(job),
            "voice_caption": None,
            "brain_voice_caption": None,
        }
    return {
        "domain": "maxillo",
        "patient": _job_patient(job),
        "brain_patient": None,
        "laparoscopy_patient": None,
        "voice_caption": _job_voice_caption(job),
        "brain_voice_caption": None,
        "laparoscopy_voice_caption": None,
    }


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
    if filename_lower.endswith(".mha"):
        return ".mha", "metaimage"
    if filename_lower.endswith(".mhd"):
        return ".mhd", "metaimage_header"
    if filename_lower.endswith(".nrrd"):
        return ".nrrd", "nrrd"
    if filename_lower.endswith(".nhdr"):
        return ".nhdr", "nrrd_header"
    if filename_lower.endswith(".zip"):
        return ".zip", "archive_zip"
    if filename_lower.endswith((".tar", ".tar.gz", ".tgz")):
        if filename_lower.endswith(".tar.gz"):
            return ".tar.gz", "archive_tar"
        if filename_lower.endswith(".tgz"):
            return ".tgz", "archive_tar"
        return ".tar", "archive_tar"
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
        # Admin-driven config (Phase 4); falls back to the historical
        # no_processing_modalities list when no config row exists.
        if not modality_requires_processing(modality_slug):
            # Create completed job
            job_obj = _create_job_if_runner_enabled(
                modality_slug,
                **_entity_fk_kwargs(patient),
                input_files={"input": key},
                status="completed",
            )
            if job_obj:
                job_obj.started_at = timezone.now()
                job_obj.completed_at = timezone.now()
                job_obj.save()
        else:
            # Create pending job for modalities that need processing
            job_obj = _create_job_if_runner_enabled(
                modality_slug,
                **_entity_fk_kwargs(patient),
                input_files={"input": key},
                status="pending",
            )
    except Exception as e:
        logger.error(f"Failed to create Job for {modality_slug}: {e}")

    return fr, job_obj


def save_generic_modality_folder(patient: Patient, modality_slug: str, folder_files):
    """Save a folder upload for an arbitrary modality slug and create a Job.
    Stores every uploaded member under one object-storage *prefix* and records them
    as a list in ``metadata['files']``, which is what makes the row a prefix row --
    see ``common.export_processing.ExportProcessor._prefix_members``.
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
    job = _create_job_if_runner_enabled(
        modality_slug,
        **_entity_fk_kwargs(patient),
        input_files={"files": [f.get("path") for f in saved_files if isinstance(f, dict)]},
    )
    return fr, job


def _validate_and_extract_nifti_orientation(cbct_file):
    """
    Validate that a CBCT uploaded file is a compressed NIfTI file (.nii.gz)
    with valid orientation metadata (qform_code >= 1 or sform_code >= 1).
    Returns the derived orientation string (e.g. 'RAS').
    Raises django.core.exceptions.ValidationError on failure.
    """
    from django.core.exceptions import ValidationError

    original_name = getattr(cbct_file, "name", "") or ""
    filename_lower = original_name.lower()
    if not filename_lower.endswith(".nii.gz"):
        raise ValidationError(
            "CBCT upload requires a compressed NIfTI file (.nii.gz). MetaImage "
            "(.mha) is not accepted; convert it to .nii.gz first."
        )

    import tempfile
    import nibabel as nib
    import numpy as np

    try:
        if hasattr(cbct_file, "seek"):
            cbct_file.seek(0)
    except Exception:
        pass

    with tempfile.NamedTemporaryFile(suffix=".nii.gz") as tmp:
        try:
            if hasattr(cbct_file, "chunks"):
                for chunk in cbct_file.chunks():
                    tmp.write(chunk)
            else:
                tmp.write(cbct_file.read())
            tmp.flush()
        except Exception as exc:
            raise ValidationError(f"Failed to read uploaded file: {exc}")

        try:
            nifti_img = nib.load(tmp.name)
        except Exception as exc:
            raise ValidationError(f"The uploaded file is not a valid NIfTI volume: {exc}")

        qform_code = int(nifti_img.header.get("qform_code", 0))
        sform_code = int(nifti_img.header.get("sform_code", 0))

        if qform_code < 1 and sform_code < 1:
            raise ValidationError(
                "CBCT file contains no orientation metadata (qform/sform codes are 0). "
                "Please specify orientation or convert before uploading."
            )

        affine = np.asarray(nifti_img.affine, dtype=np.float64)
        if not np.isfinite(affine).all() or abs(np.linalg.det(affine[:3, :3])) < 1e-9:
            raise ValidationError("CBCT file contains a degenerate or unreadable affine transform.")

        try:
            orientation_codes = nib.orientations.aff2axcodes(affine)
            orientation = "".join(orientation_codes) if orientation_codes else "unknown"
        except Exception:
            orientation = "unknown"

    try:
        if hasattr(cbct_file, "seek"):
            cbct_file.seek(0)
    except Exception:
        pass

    return orientation


def save_cbct_to_dataset(patient_or_legacy, cbct_file):
    """
    Save a CBCT upload to object storage and create the processing job.

    Accepts a compressed NIfTI, and nothing else. The platform stores ``.nii.gz``
    only, so :func:`_validate_and_extract_nifti_orientation` is the single refusal --
    it runs here, at the one point every upload path goes through (the upload page,
    the project API, replacing a patient's files), rather than in the form or the
    template where a second caller could miss it.

    Args:
        patient_or_legacy: Patient or legacy object with .patient
        cbct_file: Django UploadedFile instance

    Returns:
        tuple: (file_path, processing_job)
    """
    patient = _get_patient(patient_or_legacy)
    original_name = getattr(cbct_file, "name", "cbct.nii.gz") or "cbct.nii.gz"

    orientation = _validate_and_extract_nifti_orientation(cbct_file)

    base_filename = f"cbct_patient_{patient.patient_id}.nii.gz"

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

    key = f"{_raw_key_prefix_for(patient, 'cbct')}/{base_filename}"
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
            "file_format": "nifti_compressed",
            "needs_conversion": False,
            "orientation": orientation,
        },
    )

    # Create job only when a CBCT worker route is configured.
    processing_job = _create_job_if_runner_enabled(
        "cbct",
        **_entity_fk_kwargs(patient),
        input_files={"input": key},
    )

    return key, processing_job


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

        processing_job = _create_job_if_runner_enabled(
            "ios",
            **_entity_fk_kwargs(patient),
            input_files=input_files,
        )

        # Stage-2 dependent jobs (e.g. ios -> bite_classification) are spawned by
        # the step pipeline inside _create_job_if_runner_enabled; surface the
        # bite job for the caller's response payload.
        bite_classification_job = None
        if processing_job:
            bite_classification_job = (
                patient.jobs.filter(modality_slug=BITE_CLASSIFICATION_SLUG)
                .order_by("-created_at")
                .first()
            )

    return {
        "files": saved_files,
        "file_registries": file_registries,
        "processing_job": processing_job,
        "bite_classification_job": bite_classification_job,
    }


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
            key = f"{_raw_key_prefix_for(patient, 'intraoral-photo')}/{filename}"
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
            saved_files.append(
                {
                    "file_id": entry.id,
                    "path": key,
                    "index": idx + 1,
                }
            )
        except Exception as e:
            logger.error(f"Error saving intraoral image {idx}: {e}", exc_info=True)
            errors.append(f"Failed to save image {idx + 1}: {str(e)}")

    # Create pending job for external intraoral segmentation worker.
    job = None
    if saved_files:
        try:
            job = _create_job_if_runner_enabled(
                "intraoral-photo",
                **_entity_fk_kwargs(patient),
                input_files={str(item["file_id"]): item["path"] for item in saved_files},
                status="pending",
            )
        except Exception as e:
            logger.error(f"Error creating intraoral job: {e}", exc_info=True)

    return saved_entries, errors, job


def _record_predicted_landmarks(patient, landmark_path, job):
    """Write a landmark job's output through ``annotations/`` as well as to storage.

    The record is what the viewer, the export and the "Has IOS landmarks" filter read
    (decision #20), so a prediction that only produced an object in storage would be
    invisible to all three the moment Phase 6 stops reading documents back.

    Three rules, each a real one:

    - **``origin=PREDICTION``**, which is what keeps model output from setting the
      monotonic ``ever_annotated`` flag. It is the direct replacement for the
      ``metadata["origin"] == "ai"`` string test above -- "has a person touched this" is a
      property of the annotation record, not of a file type.
    - **A patient with human landmarks is left alone.** A revision replaces the set, so
      without this gate a nightly re-run would overwrite work somebody did by hand. The
      file half above expresses the same rule by filing to
      ``ios_landmarks_prediction``; this is that rule, on the record.
    - **Nothing here can fail the job.** A malformed document, a project with the method
      switched off, or a patient whose scan pair is gone are all reasons not to write
      landmarks -- and none of them is a reason to fail a completion the runner will retry
      forever against a gate that will not move. Phase 5 took the same line for
      segmentation.

    Reading the object here is a download on the runner-callback path, which
    ``CONTRIBUTING`` would normally push into a management command. It is accepted because
    a landmark document is kilobytes and this callback already fetched the same object to
    hash it -- not as licence for the next reader.
    """
    from annotations.adapters.ios_landmarks import by_jaw, normalize_worker_document
    from annotations.constants import AnnotationOrigin
    from annotations.services.ios_landmarks import ios_landmarks_state, save_ios_landmarks
    from maxillo.ios_meshes import current_ios_pair

    try:
        state = ios_landmarks_state(patient, domain_field="patient")
        if state["everAnnotated"]:
            logger.info(
                "Patient %s has hand-placed IOS landmarks; leaving the record alone",
                patient.patient_id,
            )
            return
        pair = current_ios_pair(patient)
        if not pair:
            logger.warning(
                "No complete IOS scan pair for patient %s; predicted landmarks have no "
                "mesh to be coordinates against",
                patient.patient_id,
            )
            return

        handle, _ = open_binary(landmark_path)
        try:
            payload = json.loads(handle.read().decode("utf-8"))
        finally:
            with contextlib.suppress(Exception):
                handle.close()
        document = normalize_worker_document(payload, patient_id=patient.patient_id)
        if not document:
            return
        jaws = by_jaw(document)
        save_ios_landmarks(
            patient,
            meshes=[
                {"file_obj": pair[jaw], "jaw": jaw, "landmarks": jaws[jaw]}
                for jaw in ("upper", "lower")
            ],
            author=None,
            # No concurrent editor to lose a race to; this is an import.
            expected_revision=state["revision"] or None,
            origin=AnnotationOrigin.PREDICTION,
            note=f"job:{job.id}",
        )
    except Exception:
        logger.exception(
            "Could not record predicted IOS landmarks for patient %s", patient.patient_id
        )


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
    output_files = dict(output_files or {})

    try:
        job = Job.objects.select_related(
            "patient", "laparoscopy_patient",
            "voice_caption", "laparoscopy_voice_caption",
        ).get(id=job_id)

        logger.info(
            f"Found job: {job.id}, modality: {job.modality_slug}, status: {job.status}"
        )
        job_patient = _job_patient(job)
        job_voice_caption = _job_voice_caption(job)

        if job.modality_slug == "cbct":
            segmentation_path = _resolve_output_path_or_key(
                output_files.get("segmentation_nifti")
            )
            if not segmentation_path or not artifact_exists(segmentation_path):
                raise ValueError(
                    "CBCT completion missing required output_files.segmentation_nifti"
                )
            output_files = {
                key: output_files[key]
                for key in (
                    "volume_nifti",
                    "segmentation_nifti",
                    "inference_stats_json",
                )
                if key in output_files
            }
            for output_name, output_spec in output_files.items():
                output_path = _resolve_output_path_or_key(output_spec)
                if not output_path or not artifact_exists(output_path):
                    raise ValueError(f"CBCT output does not exist: {output_name}")

        if job.modality_slug == "cbct_to_panoramic":
            panoramic_path = _resolve_output_path_or_key(
                output_files.get(DEFAULT_PANORAMIC_OUTPUT)
            )
            if not panoramic_path or not artifact_exists(panoramic_path):
                raise ValueError(
                    "CBCT-to-panoramic completion missing required "
                    "output_files.panoramic_png"
                )
            output_files = {
                key: value
                for key, value in output_files.items()
                if key in PANORAMIC_OUTPUT_KEYS
            }

        step = job.step or get_step(job.modality_slug)
        step_modality_slug = (
            step.modality.slug if step and step.modality_id else job.modality_slug
        )
        landmark_output_name, landmark_output = _landmark_output(output_files)

        if job.modality_slug == "ios":
            scan_outputs = {
                name: spec
                for name, spec in output_files.items()
                if name != landmark_output_name
            }
            if scan_outputs:
                arches = {_ios_output_arch(name) for name in scan_outputs}
                if not {"upper", "lower"}.issubset(arches):
                    raise ValueError(
                        "IOS completion must include both upper and lower scan outputs"
                    )
                for output_name, output_spec in scan_outputs.items():
                    output_path = _resolve_output_path_or_key(output_spec)
                    if not output_path or not artifact_exists(output_path):
                        raise ValueError(f"IOS output does not exist: {output_name}")

        if landmark_output and step_modality_slug == "ios":
            landmark_path = _resolve_output_path_or_key(landmark_output)
            if not landmark_path or not artifact_exists(landmark_path):
                raise ValueError("IOS landmark completion output does not exist")

        # IOS -> bite stage chaining is Job._pull_dependency_outputs' job: it
        # merges every completed prerequisite's outputs in, keyed by the
        # dependency's slug, when the dependent unblocks. A flat per-modality
        # pre-update here would overwrite that nested structure and drop the
        # other prerequisites' inputs (e.g. ios-landmarks' landmarks.json).

        job.mark_completed(output_files)
        logger.info(f"Job marked as completed successfully")

        # Register output files
        logger.info(f"Registering output files for modality: {job.modality_slug}")

        if job.modality_slug == "intraoral-photo":
            from annotations.constants import AnnotationOrigin
            from annotations.services import AnnotationNotAllowed
            from annotations.services.segmentation import (
                save_tooth_segmentation,
                tooth_segmentation_state,
            )

            from .intraoral_teeth import _normalize_teeth_payload

            segmentation_key = _resolve_output_path_or_key(
                (output_files or {}).get("segmentation_json")
            )
            if not segmentation_key:
                logger.error(
                    "Intraoral completion missing output_files.segmentation_json for job %s",
                    job.id,
                )
                return True

            fh, _ = open_binary(segmentation_key)
            try:
                segmentation_payload = json.loads(
                    fh.read().decode("utf-8", errors="replace")
                )
            finally:
                with contextlib.suppress(Exception):
                    fh.close()

            segmentations = segmentation_payload.get("segmentations") or {}
            valid_files = _intraoral_images_by_reference(job, segmentations.keys())

            # Confirmation now lives on the annotation *target* (annotations/0003), which
            # is where a per-image claim belongs. The skip itself is unchanged: model
            # output must never overwrite polygons somebody signed off.
            confirmations = tooth_segmentation_state(
                job_patient, domain_field="patient"
            )["confirmations"]

            images = []
            skipped_confirmed_count = 0
            for reference, teeth_payload in segmentations.items():
                row = valid_files.get(str(reference))
                if row is None:
                    logger.warning(
                        "Intraoral completion for job %s names an image it cannot "
                        "resolve: %r", job.id, reference,
                    )
                    continue
                if confirmations.get(row.id):
                    skipped_confirmed_count += 1
                    continue
                # Normalised before the adapter sees it, deliberately. `tooth_polygons`
                # *refuses* a malformed map, which is right for a person drawing and wrong
                # for model output -- one two-point ring would otherwise fail the whole
                # completion instead of being dropped, as it always has been.
                images.append(
                    {
                        "file_obj": row,
                        "teeth": _normalize_teeth_payload(teeth_payload),
                        "confirmed": False,
                    }
                )

            updated_count = 0
            output_files = dict(output_files or {})
            if images:
                try:
                    save_tooth_segmentation(
                        job_patient,
                        images=images,
                        author=None,
                        # No concurrent editor to lose a race to; this is an import.
                        expected_revision=None,
                        # `PREDICTION` is what keeps model output from setting the
                        # monotonic `ever_annotated` flag -- a prediction has never frozen
                        # a patient's raw data and must not start now.
                        origin=AnnotationOrigin.PREDICTION,
                        note=f"job:{job.id}",
                    )
                except AnnotationNotAllowed as exc:
                    # The project has tooth segmentation switched off. Recorded and
                    # skipped rather than failing the job: the pipeline did run, and a
                    # completion that errored here would be retried forever against a
                    # gate that is not going to move on its own.
                    logger.warning(
                        "Intraoral segmentation not written for job %s: %s", job.id, exc
                    )
                    output_files["segmentation_refused"] = str(exc)
                else:
                    updated_count = len(images)

            output_files["applied_segmentations"] = updated_count
            output_files["skipped_confirmed_segmentations"] = skipped_confirmed_count
            output_files["applied_views"] = _apply_intraoral_views(
                job, output_files.get("views_json")
            )
            job.output_files = output_files
            job.save(update_fields=["output_files"])
        elif job.modality_slug == "cbct_to_panoramic":
            newer_completion_exists = Job.objects.filter(
                modality_slug="cbct_to_panoramic",
                status="completed",
                created_at__gt=job.created_at,
                **_job_entity_fk_kwargs(job),
            ).exclude(id=job.id).exists()
            if newer_completion_exists:
                logger.warning(
                    "Ignoring stale CBCT-to-panoramic outputs for job %s", job.id
                )
                return True

            processed_files = {}
            for output_key, out_spec in output_files.items():
                path_or_key = _resolve_output_path_or_key(out_spec)
                if path_or_key and artifact_exists(path_or_key):
                    processed_files[str(output_key)] = {
                        "path": path_or_key,
                        "type": output_key,
                    }

            if processed_files:
                from common.models import Modality

                panoramic_modality = Modality.objects.filter(slug="panoramic").first()
                generated_rows = FileRegistry.objects.filter(
                    file_type="panoramic_processed",
                    **_job_entity_fk_kwargs(job),
                )
                generated_rows = [
                    row
                    for row in generated_rows
                    if isinstance(row.metadata, dict)
                    and row.metadata.get("generated_from") == "cbct_to_panoramic"
                ]
                if generated_rows:
                    FileRegistry.objects.filter(
                        id__in=[row.id for row in generated_rows]
                    ).delete()

                for output_key, output in processed_files.items():
                    file_size, file_hash = _size_hash_for_path_or_key(output["path"])
                    FileRegistry.objects.update_or_create(
                        file_path=output["path"],
                        defaults={
                            "file_type": "panoramic_processed",
                            "subtype": output_key,
                            "file_size": file_size or 0,
                            "file_hash": file_hash or "object",
                            "processing_job": job,
                            "modality": panoramic_modality,
                            **_job_entity_fk_kwargs(job),
                            "metadata": {
                                "processed_at": timezone.now().isoformat(),
                                "generated_from": "cbct_to_panoramic",
                                "panoramic_output": output_key,
                                "is_default": output_key == DEFAULT_PANORAMIC_OUTPUT,
                                "input_files": job.input_files or {},
                                "files": processed_files,
                                "logs": logs if logs else "",
                            },
                        },
                    )

        elif job.modality_slug != BITE_CLASSIFICATION_SLUG:
            # Generic registration: one FileRegistry row per output key, shared by
            # every modality that isn't intraoral-photo/bite_classification (both
            # genuine domain logic above/below, not naming) -- including cbct, ios,
            # video, and any future algorithm. No per-modality branch needed.
            step = job.step or get_step(job.modality_slug)
            output_modality_slug = (
                step.modality.slug if step and step.modality_id else job.modality_slug
            )
            registry_type = get_file_type_for_modality(
                output_modality_slug, is_processed=True
            )

            generic_outputs = {
                file_type: out_spec
                for file_type, out_spec in output_files.items()
                if file_type != landmark_output_name
            }
            if (
                landmark_output
                and step_modality_slug == "ios"
                and job.modality_slug != "ios"
            ):
                # Landmark stages may emit intermediate masks. They are not
                # oriented IOS meshes and must not replace the viewer pair.
                generic_outputs = {}
            generic_outputs = {
                output_name: output_spec
                for output_name, output_spec in generic_outputs.items()
                if (
                    (output_path := _resolve_output_path_or_key(output_spec))
                    and artifact_exists(output_path)
                )
            }

            # Idempotent replace only when this completion actually supplied scan
            # artifacts. A landmarks-only prediction must not remove the viewer STL.
            #
            # Scoped to rows this step produced (plus rows predating the
            # processing_job link): `registry_type` comes from the step's
            # *modality*, so sibling steps of one modality share it, and an
            # unscoped delete would make each step's completion drop the others'
            # outputs -- e.g. a step under `ios` wiping the oriented viewer pair
            # the `ios` step registered. Steps and their order are admin-editable,
            # so this must hold for any pipeline shape, not just today's.
            if generic_outputs:
                FileRegistry.objects.filter(
                    file_type=registry_type, **_job_entity_fk_kwargs(job)
                ).filter(
                    Q(processing_job__isnull=True)
                    | Q(processing_job__modality_slug=job.modality_slug)
                ).delete()

            for file_type, out_spec in generic_outputs.items():
                path_or_key = _resolve_output_path_or_key(out_spec)
                logger.info(
                    f"Processing output file: type={file_type}, path_or_key={path_or_key}"
                )
                if not path_or_key or not artifact_exists(path_or_key):
                    continue

                file_size, file_hash = _size_hash_for_path_or_key(path_or_key)
                logger.info(f"Storing FileRegistry entry with type={registry_type}")

                # subtype distinguishes multiple outputs sharing one file_type (e.g.
                # ios's upper/lower, video's compressed/subsampled, or any algorithm
                # that writes several files per job).
                FileRegistry.objects.update_or_create(
                    file_path=path_or_key,
                    defaults={
                        "file_type": registry_type,
                        "subtype": str(file_type),
                        "modality": step.modality if step else None,
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

            if output_modality_slug == "video" and generic_outputs:
                # **A derivative has to be probed, and only completion can do it.**
                # The annotator mounts the subsampled track, whose frame rate and frame
                # count are its own (one frame per source second) and nothing like the
                # raw video's -- and no browser can read either from an mp4. The upload
                # path probes because it has the bytes on disk; the cluster writes these
                # two straight into the bucket, so this is the one moment they are known
                # to exist and can be fetched once. Registration above stays generic;
                # what is domain-specific is which modality needs the question asked.
                from laparoscopy import video_probe

                for row in FileRegistry.objects.filter(
                    file_type=registry_type, processing_job=job
                ):
                    if video_probe.recorded_probe(row) is None:
                        video_probe.probe_and_record_stored(row)

            if landmark_output and step_modality_slug == "ios":
                landmark_path = _resolve_output_path_or_key(landmark_output)
                if not landmark_path or not artifact_exists(landmark_path):
                    logger.warning("IOS landmark output does not exist for job %s", job.id)
                elif not job_patient:
                    logger.warning("IOS landmark output has no patient for job %s", job.id)
                else:
                    file_size, file_hash = _size_hash_for_path_or_key(landmark_path)
                    active = FileRegistry.objects.filter(
                        file_type="ios_landmarks", **_job_entity_fk_kwargs(job)
                    ).order_by("-created_at", "-id").first()
                    defaults = {
                        "file_size": file_size or 0,
                        "file_hash": file_hash or "object",
                        "subtype": "landmarks",
                        "modality": step.modality if step else None,
                        "processing_job": job,
                        **_job_entity_fk_kwargs(job),
                        "metadata": {
                            "origin": "ai",
                            "source_job_id": job.id,
                            "processed_at": timezone.now().isoformat(),
                        },
                    }
                    human_edited = (active and (active.metadata or {}).get("origin") != "ai")
                    if human_edited:
                        FileRegistry.objects.update_or_create(
                            file_path=landmark_path,
                            defaults={**defaults, "file_type": "ios_landmarks_prediction"},
                        )
                    else:
                        if active:
                            active.delete()
                        FileRegistry.objects.update_or_create(
                            file_path=landmark_path,
                            defaults={**defaults, "file_type": "ios_landmarks"},
                        )
                    _record_predicted_landmarks(job_patient, landmark_path, job)

        # Update related model status
        logger.info(f"Updating related model status for modality: {job.modality_slug}")
        if job_patient and job.modality_slug == BITE_CLASSIFICATION_SLUG:
            logger.info(
                f"Bite classification job completed for patient {getattr(job_patient, 'patient_id', 'unknown')}"
            )

            try:
                classification_file = None
                for file_type, out_spec in output_files.items():
                    path_or_key = _resolve_output_path_or_key(out_spec)
                    basename = os.path.basename(str(path_or_key)).lower()
                    if (
                        basename == "predictions.json"
                        or str(path_or_key).endswith("_bite_classification_results.json")
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

                    # Bits2Bites emits per-task class indices; older/hand-made
                    # results carry the five fields directly.
                    if isinstance(classification_data, list):
                        values = bite_classification_values(
                            classification_data,
                            getattr(job_patient, "patient_id", None),
                        )
                        if values is None:
                            raise ValueError(
                                "bite predictions do not identify this patient"
                            )
                    else:
                        values = {
                            field: classification_data.get(field, "Unknown")
                            for field in (
                                "sagittal_left",
                                "sagittal_right",
                                "vertical",
                                "transverse",
                                "midline",
                            )
                        }

                    sagittal_left = values["sagittal_left"]
                    sagittal_right = values["sagittal_right"]
                    vertical = values["vertical"]
                    transverse = values["transverse"]
                    midline = values["midline"]

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
            "patient", "laparoscopy_patient",
            "voice_caption","laparoscopy_voice_caption",
        ).get(id=job_id)

        job_patient = _job_patient(job)
        job_voice_caption = _job_voice_caption(job)
        job.mark_failed(error_msg, can_retry)

        if job_patient and job.modality_slug == BITE_CLASSIFICATION_SLUG:
            logger.info(
                f"Bite classification job failed for patient {getattr(job_patient, 'patient_id', 'unknown')}"
            )

        return True

    except Job.DoesNotExist:
        return False
