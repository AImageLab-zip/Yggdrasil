"""Brain's part of the upload API: one NIfTI volume per MRI modality."""

from common.domain_uploads import PerModalityHandler, UploadField

from .file_utils import save_brain_modality_file

# Field name == modality slug, as on the web upload form (brain/views.py).
BRAIN_UPLOAD_SLUGS = (
    "braintumor-mri-t1",
    "braintumor-mri-t2",
    "braintumor-mri-flair",
    "braintumor-mri-t1c",
    "braintumor-mri-seg",
)


class BrainUploadHandler(PerModalityHandler):
    upload_fields = tuple(UploadField(slug, slug) for slug in BRAIN_UPLOAD_SLUGS)

    def save_one(self, patient, spec, uploaded_file, files):
        return save_brain_modality_file(patient, spec.modality, uploaded_file)
