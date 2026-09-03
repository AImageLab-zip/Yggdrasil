"""What an annotation *means*: the label vocabularies, versioned.

A dense segmentation is a grid of small integers. Nothing in the voxels says
that ``2`` is "mandible" -- that lives here, and it has to keep living here
unchanged for as long as the labelmap exists. The uniqueness constraint below is
not bookkeeping: it is the statement "an integer ``2`` in an old labelmap must
never come to mean something else", written where the database can enforce it
rather than in a comment somebody has to remember.

Schemas are versioned rather than edited for the same reason. Changing the
meaning of a value inside a live schema would silently reinterpret every
labelmap that references it; publishing a new version leaves the old rows
pointing at the old meanings.
"""

from django.contrib.auth.models import User
from django.db import models


class LabelSchema(models.Model):
    """A named, versioned set of label definitions.

    ``domain`` is blank for a vocabulary that applies everywhere and set to one
    domain slug otherwise, matching ``common.AnnotationMethod``.
    """

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120)
    version = models.PositiveIntegerField(
        default=1,
        help_text="Bump instead of editing: a published version's values are frozen.",
    )
    description = models.TextField(blank=True)
    domain = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Blank for a schema usable in every domain.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_label_schemas",
    )

    class Meta:
        db_table = "annotations_label_schema"
        ordering = ["domain", "slug", "version"]
        constraints = [
            models.UniqueConstraint(
                fields=["slug", "version"], name="annotations_uniq_schema_slug_version"
            ),
        ]

    def __str__(self):
        return f"{self.slug} v{self.version}"


class LabelDefinition(models.Model):
    """One label inside a schema: an integer value with a fixed meaning.

    ``value`` is what appears in a labelmap's voxels. ``code`` is the external
    identifier where one exists -- an FDI tooth number, a SNOMED code -- and is
    what an adapter should match on when importing, because an external system's
    numbering is its own.
    """

    schema = models.ForeignKey(
        LabelSchema, on_delete=models.CASCADE, related_name="definitions"
    )
    value = models.PositiveIntegerField(
        help_text="The integer written into labelmaps. Immutable once published.",
    )
    code = models.CharField(
        max_length=60,
        null=True,
        blank=True,
        default=None,
        help_text="External identifier (e.g. an FDI tooth code). NULL if none.",
    )
    display_name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    color = models.CharField(
        max_length=7,
        blank=True,
        help_text="#rrggbb suggestion for viewers; not authoritative.",
    )
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(
        default=True,
        help_text="Retired labels stay readable; deleting one would orphan voxels.",
    )

    class Meta:
        db_table = "annotations_label_definition"
        ordering = ["schema_id", "order", "value"]
        constraints = [
            # The one that matters. See the module docstring.
            models.UniqueConstraint(
                fields=["schema", "value"], name="annotations_uniq_label_schema_value"
            ),
            # A code, where present, addresses exactly one label in a schema --
            # otherwise an FDI-keyed import has no deterministic target. ``code``
            # is nullable rather than blank precisely so this holds: MySQL drops
            # a conditional constraint silently (F12), but it does treat NULLs as
            # distinct, so unlabelled rows coexist while real codes stay unique.
            models.UniqueConstraint(
                fields=["schema", "code"], name="annotations_uniq_label_schema_code"
            ),
        ]
        indexes = [
            models.Index(fields=["schema", "is_active"]),
        ]

    def __str__(self):
        return f"{self.schema_id}:{self.value} {self.display_name}"
