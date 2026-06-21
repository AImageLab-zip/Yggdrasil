import os
import logging

from django.utils import timezone

from common.models import FileRegistry, Job
from maxillo.file_utils import (
    _raw_key_prefix_for,
    _upload_uploaded_file_to_storage,
    _entity_fk_kwargs,
)

logger = logging.getLogger(__name__)


def save_video_to_dataset(patient, video_file):
    """Upload a video file and create a pending processing job with the standard video payload.

    Returns:
        tuple: (FileRegistry | None, Job | None)
    """
    original_name = video_file.name
    ext = os.path.splitext(original_name)[1].lower() or ".mp4"
    filename = f"video_patient_{patient.patient_id}{ext}"
    key = f"{_raw_key_prefix_for(patient, 'video')}/{filename}"
    key, file_size, file_hash = _upload_uploaded_file_to_storage(
        key=key, uploaded_file=video_file
    )

    modality_fk = None
    try:
        from common.models import Modality as _Modality
        modality_fk = _Modality.objects.filter(slug="video").first()
    except Exception:
        pass

    try:
        fr = FileRegistry.objects.create(
            file_type="video_raw",
            file_path=key,
            file_size=file_size,
            file_hash=file_hash,
            **_entity_fk_kwargs(patient),
            modality=modality_fk,
            metadata={
                "original_filename": original_name,
                "uploaded_at": timezone.now().isoformat(),
                "modality_slug": "video",
            },
        )
    except Exception:
        logger.exception("Failed to create FileRegistry for video; proceeding to create Job anyway")
        fr = None

    job_obj = None
    try:
        job_obj = Job.objects.create(
            modality_slug="video",
            **_entity_fk_kwargs(patient),
            input_file_path=key,
            status="pending",
            output_files={
                "schema_version": 1,
                "input_type": "video",
                "processing_profile": "laparoscopy_video_v1",
                "expected_outputs": ["compressed", "subsampled"],
                "derivatives": {
                    "compressed": {
                        "type": "video",
                        "make_primary": True,
                        "container": "mp4",
                        "video_codec": "h264",
                    },
                    "subsampled": {
                        "type": "video",
                        "make_primary": False,
                        "container": "mp4",
                        "video_codec": "h264",
                        "sampling": {
                            "mode": "fps",
                            "target_fps": 1.0,
                        },
                    },
                },
            },
        )
    except Exception as e:
        logger.error(f"Failed to create Job for video: {e}")

    return fr, job_obj
