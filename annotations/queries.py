"""Read-only questions about annotation work that more than one caller asks.

Not a service: nothing here writes, and ``annotations.services`` is explicit that read
paths are the models' job. This module exists because the questions below were each
being asked from more than one place, and none of them is obvious enough to retype --
"does this patient still have tooth polygons" means *on the latest revision*, and a
second copy of that reasoning is a second copy to get wrong.

Before this, the presence predicate was ``intraoral_segmentations__isnull=False`` written
twice, in ``maxillo.views.patient_list`` and ``common.export_catalog``. They now disagree
with each other only if this module is wrong for both.
"""

from django.db.models import Exists, OuterRef, Subquery

from annotations.adapters.tooth_segmentation import SEGMENTATION_KIND


def _latest_revisions_with_geometry():
    """Revisions that are the newest on their set *and* still hold 2D geometry.

    Only the latest revision counts, and that is the whole point rather than an
    optimisation. A set is created by the first save and lives forever; a revision that
    deleted every polygon is a legitimate state, and older revisions keep their items as
    the audit trail. Matching on any revision would answer "has ever been segmented" --
    which is what ``ever_annotated`` is for, and is a different question -- and would
    resurrect deleted work in a filter while the editor showed none.
    """
    from annotations.models import AnnotationRevision

    latest_number = (
        AnnotationRevision.objects.filter(annotation_set=OuterRef("annotation_set"))
        .order_by("-revision_number")
        .values("revision_number")[:1]
    )
    return AnnotationRevision.objects.filter(
        annotation_set__kind=SEGMENTATION_KIND,
        revision_number=Subquery(latest_number),
        geometry2ditems__isnull=False,
    )


def with_tooth_segmentation(patients):
    """Narrow a maxillo ``Patient`` queryset to those that still have tooth polygons.

    :param patients: a ``maxillo.Patient`` queryset.
    :returns: the same queryset, narrowed.
    """
    return patients.filter(
        Exists(
            _latest_revisions_with_geometry().filter(
                annotation_set__patient=OuterRef("pk")
            )
        )
    )


def tooth_segmentation_image_count(patients):
    """How many photographs across these patients still have polygons.

    One per document :meth:`~common.export_processing.ExportProcessor._collect_tooth_segmentation`
    yields, which is what makes it the right number for an export preview: a preview that
    promises N files and produces N-1 is how an export looks broken to whoever asked for
    it.

    :param patients: a maxillo ``Patient`` queryset.
    """
    from annotations.models import Geometry2DItem

    return (
        Geometry2DItem.objects.filter(
            revision__in=_latest_revisions_with_geometry(),
            revision__annotation_set__patient__in=patients,
        )
        .values("target_id")
        .distinct()
        .count()
    )
