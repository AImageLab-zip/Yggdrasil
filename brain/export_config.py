"""Brain export configuration.

The modality -> file_type mapping this module used to own now lives in
``common.export_catalog`` (one declarative catalog for all three domains, after
the three copies of this mapping had drifted apart). The dict is kept as a thin
view over the catalog because ``brain.apps`` and ``brain.views`` still call the
install hook at startup, and external callers may read the mapping.
"""

from common.export_catalog import (
    BUCKET_PROCESSED,
    BUCKET_RAW,
    artifacts_for_domain,
)


def _mapping():
    mapping = {}
    for artifact in artifacts_for_domain("brain"):
        if not artifact.modality or not artifact.is_file_backed:
            continue
        groups = mapping.setdefault(artifact.modality, {"raw": [], "processed": []})
        if artifact.bucket == BUCKET_RAW:
            groups["raw"].extend(artifact.file_types)
        elif artifact.bucket == BUCKET_PROCESSED:
            groups["processed"].extend(artifact.file_types)
    return mapping


BRAIN_EXPORT_MODALITY_FILE_TYPES = _mapping()


def install_brain_export_mappings():
    """Compatibility hook kept for app startup."""
    return BRAIN_EXPORT_MODALITY_FILE_TYPES
