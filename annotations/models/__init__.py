"""The durable annotation model.

Split across four modules by role -- labels, resources, the set/revision spine,
and the items themselves -- and re-exported here so callers import from
``annotations.models`` and never have to know which file a model lives in.
"""

from annotations.models.items import (
    EventAnnotationItem,
    Geometry2DItem,
    MeasurementItem,
    SpatialAnnotation3DItem,
    TemporalAnnotationItem,
)
from annotations.models.labels import LabelDefinition, LabelSchema
from annotations.models.resources import SourceResource
from annotations.models.sets import (
    AnnotationPayload,
    AnnotationRevision,
    AnnotationSelector,
    AnnotationSet,
    AnnotationTarget,
)

__all__ = [
    "AnnotationPayload",
    "AnnotationRevision",
    "AnnotationSelector",
    "AnnotationSet",
    "AnnotationTarget",
    "EventAnnotationItem",
    "Geometry2DItem",
    "LabelDefinition",
    "LabelSchema",
    "MeasurementItem",
    "SourceResource",
    "SpatialAnnotation3DItem",
    "TemporalAnnotationItem",
]
