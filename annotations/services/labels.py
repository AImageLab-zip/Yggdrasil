"""Resolving the seeded label vocabularies.

One module because there are now two callers -- tooth polygons and IOS landmarks both
resolve FDI codes -- and "refuse rather than create at runtime" is a rule that stops being
a rule the moment there are two copies of it to keep in step.
"""

from django.core.exceptions import ValidationError

from annotations.adapters.tooth_segmentation import FDI_SCHEMA_SLUG, FDI_SCHEMA_VERSION
from annotations.models import LabelSchema


def fdi_schema():
    """The seeded FDI vocabulary.

    Refused loudly rather than created on demand: the integers are frozen by
    ``UniqueConstraint(schema, value)`` and a schema conjured at runtime would get a
    numbering nobody reviewed, under the same slug, meaning something else.
    """
    schema = LabelSchema.objects.filter(
        slug=FDI_SCHEMA_SLUG, version=FDI_SCHEMA_VERSION
    ).first()
    if schema is None:
        raise ValidationError(
            f"the {FDI_SCHEMA_SLUG} v{FDI_SCHEMA_VERSION} label schema is missing; it is "
            "seeded by annotations/migrations/0002 and must not be created at runtime"
        )
    return schema
