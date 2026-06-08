"""Brain-specific file handling helpers."""

import hashlib
import os

from common.models import FileRegistry, Job, Modality
from common.object_storage import get_object_storage
from django.utils import timezone


BRAIN_NO_PROCESSING_MODALITIES = {
    "braintumor-mri-t1",
    "braintumor-mri-t2",
    "braintumor-mri-flair",
    "braintumor-mri-t1c",
    "braintumor-mri-seg",
}


def _detect_extension_and_format(filename_lower: str):
    if filename_lower.endswith(".nii.gz"):
        return ".nii.gz", "nifti_compressed"
    if filename_lower.endswith(".nii"):
        return ".nii", "nifti"
    if filename_lower.endswith((".dcm", ".dicom")):
        return ".dcm", "dicom_single"
    return os.path.splitext(filename_lower)[1] or ".bin", "unknown"


def _file_type_for_modality(modality_slug, is_processed=False):
    suffix = "_processed" if is_processed else "_raw"
    file_type = modality_slug.replace("-", "_") + suffix
    valid_file_types = FileRegistry.get_file_type_choices_dict().keys()
    return file_type if file_type in valid_file_types else "generic_processed" if is_processed else "generic_raw"


def _upload_uploaded_file_to_storage(key, uploaded_file):
    uploaded_file.seek(0)
    hasher = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        hasher.update(chunk)
    file_size = uploaded_file.size
    file_hash = hasher.hexdigest()

    uploaded_file.seek(0)
    get_object_storage().upload_fileobj(
        uploaded_file,
        key=key,
        content_type=getattr(uploaded_file, "content_type", None),
        metadata={
            "original_filename": getattr(uploaded_file, "name", ""),
            "sha256": file_hash,
        },
    )
    return key, file_size, file_hash


def save_brain_modality_file(patient, modality_slug, uploaded_file):
    """Save a Brain modality file and mark passive MRI uploads as completed."""
    original_name = uploaded_file.name
    extension, file_format = _detect_extension_and_format(original_name.lower())
    filename = f"{modality_slug}_patient_{patient.patient_id}{extension}"
    key = f"brain/patients/{patient.patient_id}/raw/{modality_slug}/{filename}"
    key, file_size, file_hash = _upload_uploaded_file_to_storage(key, uploaded_file)

    modality = Modality.objects.filter(slug=modality_slug).first()
    file_registry = FileRegistry.objects.create(
        domain="brain",
        brain_patient=patient,
        file_type=_file_type_for_modality(modality_slug, is_processed=False),
        file_path=key,
        file_size=file_size,
        file_hash=file_hash,
        modality=modality,
        metadata={
            "original_filename": original_name,
            "uploaded_at": timezone.now().isoformat(),
            "file_format": file_format,
            "modality_slug": modality_slug,
        },
    )

    status = "completed" if modality_slug in BRAIN_NO_PROCESSING_MODALITIES else "pending"
    job = Job.objects.create(
        domain="brain",
        brain_patient=patient,
        modality_slug=modality_slug,
        input_file_path=key,
        status=status,
        output_files={"input_format": file_format, "file_path": key}
        if status == "completed"
        else {"input_format": file_format, "expected_outputs": []},
    )

    if job and modality_slug in BRAIN_NO_PROCESSING_MODALITIES:
        job.started_at = job.started_at or timezone.now()
        job.completed_at = timezone.now()
        job.save(update_fields=["started_at", "completed_at"])

    return file_registry, job
