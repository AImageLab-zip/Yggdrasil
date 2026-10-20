"""Urology's part of the upload API: one file per modality, plus an optional
segmentation overlay sent as ``<slug>-segmentation``."""

from common.domain_uploads import PerModalityHandler, UploadField

from .file_utils import save_urology_modality_file, save_urology_segmentation_file

UROLOGY_UPLOAD_SLUGS = ("urology-mri", "urology-wsi", "urology-confocal")


class UrologyUploadHandler(PerModalityHandler):
    upload_fields = tuple(
        UploadField(slug, slug, companions=(f"{slug}-segmentation",))
        for slug in UROLOGY_UPLOAD_SLUGS
    )

    def save_one(self, patient, spec, uploaded_file, files):
        file_registry, job = save_urology_modality_file(patient, spec.modality, uploaded_file)
        segmentation = files.get(f"{spec.modality}-segmentation")
        if segmentation and file_registry:
            save_urology_segmentation_file(
                patient, spec.modality, segmentation[0], parent_file=file_registry
            )
        return file_registry, job
