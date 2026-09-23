"""Urology export configuration."""

from common.export_catalog import (
    BUCKET_PROCESSED,
    BUCKET_RAW,
    artifacts_for_domain,
)


def _mapping():
    mapping = {}
    for artifact in artifacts_for_domain("urology"):
        if not artifact.modality or not artifact.is_file_backed:
            continue
        groups = mapping.setdefault(artifact.modality, {"raw": [], "processed": []})
        if artifact.bucket == BUCKET_RAW:
            groups["raw"].extend(artifact.file_types)
        elif artifact.bucket == BUCKET_PROCESSED:
            groups["processed"].extend(artifact.file_types)
    return mapping


UROLOGY_EXPORT_MODALITY_FILE_TYPES = _mapping()


def install_urology_export_mappings():
    """Compatibility hook kept for app startup."""
    return UROLOGY_EXPORT_MODALITY_FILE_TYPES
