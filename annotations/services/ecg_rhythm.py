"""Saving and reading an ECG's rhythm call: AF, NSR, Other or NI.

One categorical statement about the whole recording, so it is one
``EventAnnotationItem`` with no target -- the same shape an occlusion facet takes
(``annotations.adapters.legacy_maxillo.occlusion_classification``). Every change
is a new revision: the previous call and who made it stay on record, and two
annotators who both loaded revision 3 cannot silently overwrite each other --
the second save gets :class:`AnnotationConflict` and has to reload.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from annotations.constants import AnnotationOrigin
from annotations.services.items import add_event
from annotations.services.sets import get_or_create_set, record_revision
from common.models import AnnotationMethod

#: The ``AnnotationSet.kind`` this work is filed under.
RHYTHM_KIND = "ecg_rhythm_classification"

#: The ``AnnotationMethod`` slug that gates it (``setup_cardiology_modalities``).
RHYTHM_METHOD_SLUG = "ecg_classification"

#: The event every revision carries exactly one of.
RHYTHM_EVENT = "ecg.rhythm"

#: Code -> label, in the order the page offers them.
RHYTHM_CHOICES = (
    ("AF", "AF"),
    ("NSR", "NSR"),
    ("Other", "Other"),
    ("NI", "NI"),
)
RHYTHM_LABELS = dict(RHYTHM_CHOICES)


def rhythm_method():
    """The gating ``AnnotationMethod``, or ``None`` (ungated) if it is not registered."""
    return AnnotationMethod.objects.filter(slug=RHYTHM_METHOD_SLUG).first()


@transaction.atomic
def save_ecg_rhythm(patient, value, *, author, expected_revision):
    """Record ``value`` as the patient's rhythm call.

    :param expected_revision: the revision number the caller loaded (0 for none).
    :raises ValidationError: ``value`` is not one of :data:`RHYTHM_CHOICES`.
    :raises AnnotationNotAllowed: the patient's project has the method switched off.
    :raises AnnotationConflict: somebody saved since ``expected_revision``.
    :returns: the new state, as :func:`ecg_rhythm_state` reports it.
    """
    if value not in RHYTHM_LABELS:
        raise ValidationError(f"value must be one of {list(RHYTHM_LABELS)}")

    annotation_set = get_or_create_set(
        patient,
        RHYTHM_KIND,
        annotation_method=rhythm_method(),
        created_by=author,
    )
    revision = record_revision(
        annotation_set,
        expected_revision=expected_revision,
        author=author,
        origin=AnnotationOrigin.MANUAL,
    )
    add_event(revision, None, event_type=RHYTHM_EVENT, value=value)
    return ecg_rhythm_state(patient)


def ecg_rhythm_state(patient):
    """The current call and the revision a save must quote.

    Only the latest revision counts; older ones are the audit trail.

    :returns: ``{"revision": int, "value": str, "label": str, "annotator": User|None,
        "timestamp": datetime|None}`` -- ``value`` is ``""`` when there is no call.
    """
    from annotations.models import AnnotationSet, EventAnnotationItem
    from common.domains import fk_fields_for

    patient_fk, _ = fk_fields_for(patient._meta.app_label)
    empty = {"revision": 0, "value": "", "label": "", "annotator": None, "timestamp": None}

    annotation_set = (
        AnnotationSet.objects.filter(**{patient_fk: patient, "kind": RHYTHM_KIND})
        .order_by("id")
        .first()
    )
    if annotation_set is None:
        return empty
    revision = (
        annotation_set.revisions.select_related("author").order_by("-revision_number").first()
    )
    if revision is None:
        return empty
    item = EventAnnotationItem.objects.filter(revision=revision, event_type=RHYTHM_EVENT).first()
    value = item.value if item else ""
    return {
        "revision": revision.revision_number,
        "value": value,
        "label": RHYTHM_LABELS.get(value, value),
        "annotator": revision.author,
        "timestamp": revision.created_at,
    }
