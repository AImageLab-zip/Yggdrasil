"""Laparoscopy's part of the upload API: one surgical video."""

from common.domain_uploads import PerModalityHandler, UploadField, UploadRejected

from .file_utils import save_video_to_dataset


class LaparoscopyUploadHandler(PerModalityHandler):
    upload_fields = (UploadField("video", "video"),)

    def save_one(self, patient, spec, uploaded_file, files):
        file_registry, job = save_video_to_dataset(patient, uploaded_file)
        if file_registry is None:
            raise UploadRejected("Video file could not be saved (storage may be unavailable).")
        return file_registry, job
