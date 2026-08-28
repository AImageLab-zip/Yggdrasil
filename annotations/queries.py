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

from annotations.adapters.ios_landmarks import LANDMARKS_KIND
from annotations.adapters.tooth_segmentation import SEGMENTATION_KIND


def _latest_revisions(kind, **holds):
    """Revisions that are the newest on their set *and* still hold items of some type.

    Only the latest revision counts, and that is the whole point rather than an
    optimisation. A set is created by the first save and lives forever; a revision that
    deleted every item is a legitimate state, and older revisions keep theirs as the audit
    trail. Matching on any revision would answer "has ever been annotated" -- which is what
    ``ever_annotated`` is for, and is a different question -- and would resurrect deleted
    work in a filter while the editor showed none.

    :param holds: the reverse accessor that must be non-empty, e.g.
        ``geometry2ditems__isnull=False``. Naming it per surface rather than checking "any
        item" keeps a landmark from answering a question about polygons.
    """
    from annotations.models import AnnotationRevision

    latest_number = (
        AnnotationRevision.objects.filter(annotation_set=OuterRef("annotation_set"))
        .order_by("-revision_number")
        .values("revision_number")[:1]
    )
    return AnnotationRevision.objects.filter(
        annotation_set__kind=kind,
        revision_number=Subquery(latest_number),
        **holds,
    )


def _latest_revisions_with_geometry():
    """Revisions that are the newest on their set *and* still hold 2D geometry.

    Only the latest revision counts, and that is the whole point rather than an
    optimisation. A set is created by the first save and lives forever; a revision that
    deleted every polygon is a legitimate state, and older revisions keep their items as
    the audit trail. Matching on any revision would answer "has ever been segmented" --
    which is what ``ever_annotated`` is for, and is a different question -- and would
    resurrect deleted work in a filter while the editor showed none.
    """
    return _latest_revisions(SEGMENTATION_KIND, geometry2ditems__isnull=False)


def _latest_revisions_with_landmarks():
    """Revisions that are the newest on their set *and* still hold 3D landmark points."""
    return _latest_revisions(LANDMARKS_KIND, spatialannotation3ditems__isnull=False)


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


def with_ios_landmarks(patients):
    """Narrow a maxillo ``Patient`` queryset to those that still have IOS landmarks.

    Replaces ``files__file_type='ios_landmarks'``, which was written twice -- in
    ``maxillo.views.patient_list`` and ``common.export_catalog`` -- and which asked a
    question about object storage rather than about annotation work. The two differ for a
    patient whose landmarks were all deleted: the file row survives as history, and the
    filter should not.

    :param patients: a ``maxillo.Patient`` queryset.
    :returns: the same queryset, narrowed.
    """
    return patients.filter(
        Exists(
            _latest_revisions_with_landmarks().filter(
                annotation_set__patient=OuterRef("pk")
            )
        )
    )


def ios_landmarks_tooth_count(patients):
    """How many teeth across these patients still carry landmarks.

    Counted over ``(target, label)`` rather than rows, because one tooth owns several
    points -- eight single-point types plus however many cusps -- and a preview promising
    one file per *point* would be wrong by an order of magnitude.

    :param patients: a maxillo ``Patient`` queryset.
    """
    from annotations.models import SpatialAnnotation3DItem

    return (
        SpatialAnnotation3DItem.objects.filter(
            revision__in=_latest_revisions_with_landmarks(),
            revision__annotation_set__patient__in=patients,
        )
        .values("target_id", "label_id")
        .distinct()
        .count()
    )
