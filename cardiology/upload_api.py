"""Cardiology's part of the upload API: one ECG JSON recording."""

from common.domain_uploads import PerModalityHandler, UploadField, UploadRejected

from .file_utils import InvalidEcgFile, save_ecg_to_dataset


class CardiologyUploadHandler(PerModalityHandler):
    upload_fields = (UploadField("ecg", "ecg"),)

    def save_one(self, patient, spec, uploaded_file, files):
        try:
            return save_ecg_to_dataset(patient, uploaded_file), None
        except InvalidEcgFile as exc:
            raise UploadRejected(str(exc)) from exc
