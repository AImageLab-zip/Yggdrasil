"""The only layer that writes annotation data.

Views, adapters and management commands go through these functions; none of them
import a model and call ``.save()``. That is not a style preference. Every write
has to allocate a revision number against the unique constraint, refresh the
monotonic ``ever_annotated`` flag, fingerprint the targets and validate the
items -- all in one transaction. Any caller free to skip a step will eventually
skip the flag, and then a scan with landmarks on it becomes replaceable.

Read paths do not belong here: querying is what the models are for.
"""

from annotations.services.apply import apply_descriptors
from annotations.services.exceptions import (
    AnnotationConflict,
    AnnotationLocked,
    AnnotationNotAllowed,
    AnnotationServiceError,
)
from annotations.services.items import (
    add_event,
    add_geometry_2d,
    add_measurement,
    add_spatial_3d,
    add_temporal,
)
from annotations.services.resources import (
    fingerprint_targets,
    register_derived,
    register_file,
    register_logical_volume,
)
from annotations.services.viewer import (
    MAX_ANNOTATIONS_PER_REVISION,
    MEASUREMENTS_KIND,
    save_measurements,
)
from annotations.services.sets import (
    add_payload,
    attach_target,
    confirm,
    current_revision_number,
    get_or_create_set,
    record_revision,
)

__all__ = [
    "MAX_ANNOTATIONS_PER_REVISION",
    "MEASUREMENTS_KIND",
    "AnnotationConflict",
    "AnnotationLocked",
    "AnnotationNotAllowed",
    "AnnotationServiceError",
    "add_event",
    "add_geometry_2d",
    "add_measurement",
    "add_payload",
    "add_spatial_3d",
    "add_temporal",
    "apply_descriptors",
    "attach_target",
    "confirm",
    "current_revision_number",
    "fingerprint_targets",
    "get_or_create_set",
    "record_revision",
    "register_derived",
    "register_file",
    "register_logical_volume",
    "save_measurements",
]
