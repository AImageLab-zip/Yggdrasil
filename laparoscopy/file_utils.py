import os
import logging

from django.utils import timezone

from common.models import FileRegistry
from common.uploads import (
    raw_key_prefix_for,
    upload_uploaded_file_to_storage,
    entity_fk_kwargs,
)
from laparoscopy import video_probe
from maxillo.file_utils import _create_job_if_runner_enabled

logger = logging.getLogger(__name__)


def save_video_to_dataset(patient, video_file):
    """Upload a video file and create a pending processing job with the standard video payload.

    Returns:
        tuple: (FileRegistry | None, Job | None)
    """
    original_name = video_file.name
    ext = os.path.splitext(original_name)[1].lower() or ".mp4"
    filename = f"video_patient_{patient.patient_id}{ext}"
    key = f"{raw_key_prefix_for(patient, 'video')}/{filename}"
    key, file_size, file_hash = upload_uploaded_file_to_storage(
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
            **entity_fk_kwargs(patient),
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

    if fr is not None:
        # The frame rate and frame size are properties of these bytes that a browser
        # cannot read, and the annotator refuses to mount without them rather than
        # guess 30 fps for a 25 fps recording. This is the moment the file is on
        # local disk, so this is where the question is asked. It never fails the
        # upload -- see probe_and_record_upload.
        video_probe.probe_and_record_upload(fr, video_file)

    job_obj = None
    try:
        # **The same helper every other modality's upload uses.** This built its Job by
        # hand, which meant `is_runner_enabled_for_modality` was never consulted -- a
        # project with the video step switched off still queued work nothing would claim
        # -- and `create_step_jobs` was re-implemented below it.
        #
        # `output_files` used to be pre-seeded here with a `derivatives` manifest, from
        # the Docker/Celery runner the algorithm was first written against. The cluster
        # contract has no manifest: `run.sbatch` is handed the input keys and an output
        # prefix, and `common/runner/run.py::_collect_output_files` *discovers* what was
        # written. Seeding it was worse than redundant -- `_job_pre_save` clears
        # `output_files` on dispatch, so the shape only ever existed between two lines of
        # this function, while reading as a contract somebody could rely on. What the
        # algorithm produces is stated in `algo/laparoscopy/run.sbatch`, once.
        job_obj = _create_job_if_runner_enabled(
            "video",
            **entity_fk_kwargs(patient),
            input_files={"input": key},
            status="pending",
        )
    except Exception as e:
        logger.error(f"Failed to create Job for video: {e}")

    return fr, job_obj
