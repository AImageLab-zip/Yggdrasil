"""Brain-specific export modality mappings."""


BRAIN_EXPORT_MODALITY_FILE_TYPES = {
    "braintumor-mri-t1": {
        "raw": ["braintumor_mri_t1_raw"],
        "processed": ["braintumor_mri_t1_processed"],
    },
    "braintumor-mri-t1c": {
        "raw": ["braintumor_mri_t1c_raw"],
        "processed": ["braintumor_mri_t1c_processed"],
    },
    "braintumor-mri-t2": {
        "raw": ["braintumor_mri_t2_raw"],
        "processed": ["braintumor_mri_t2_processed"],
    },
    "braintumor-mri-flair": {
        "raw": ["braintumor_mri_flair_raw"],
        "processed": ["braintumor_mri_flair_processed"],
    },
    "braintumor-mri-seg": {
        "raw": ["braintumor_mri_seg_raw"],
        "processed": ["braintumor_mri_seg_processed"],
    },
}


def install_brain_export_mappings():
    """Compatibility hook kept for app startup."""
    return BRAIN_EXPORT_MODALITY_FILE_TYPES
