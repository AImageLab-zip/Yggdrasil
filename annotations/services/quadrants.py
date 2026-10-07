"""Video quadrant markers: "from here on, the camera is in quadrant X".

One ``AnnotationSet`` of kind ``video_quadrants`` per patient. A marker is one
``EventAnnotationItem`` (``event_type="quadrant"``, ``time_ms``, ``label`` = a
``LabelDefinition``). A marker is an *instant*; the span it covers runs to the next marker
or the end of the video and is derived by the reader, so editing one marker never rewrites
its neighbour.

The vocabulary is its own per-project ``LabelSchema`` (``quadrant-project-<pk>``), separate
from the segmentation labels: a quadrant is a state over time, a region is a mask, and
sharing a schema would tie their lifecycles together. The label CRUD is the segmentation
one, parameterised by :data:`VOCABULARY`.

A save replaces the whole marker list (a revision is a snapshot). It is gated by the same
``image_segmentation`` annotation method as the masks: both live behind Annotation Mode.
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from annotations.constants import AnnotationOrigin
from annotations.models import AnnotationSet
from annotations.services import image_segmentation as labels
from annotations.services.items import add_event
from annotations.services.resources import register_file
from annotations.services.sets import attach_target, get_or_create_set, record_revision
from annotations.validators.image_segmentation import validate_time_ms
from common.domains import fk_fields_for

KIND = "video_quadrants"
EVENT = "quadrant"
VOCABULARY = "quadrant"


def list_labels(project):
    return labels.list_labels(project, VOCABULARY)


def create_label(project, *, name, color=""):
    return labels.create_label(project, name=name, color=color, vocabulary=VOCABULARY)


def update_label(project, code, **changes):
    return labels.update_label(project, code, vocabulary=VOCABULARY, **changes)


def _find_set(patient):
    domain = patient._meta.app_label
    patient_fk = fk_fields_for(domain)[0]
    return (
        AnnotationSet.objects.filter(domain=domain, kind=KIND, **{patient_fk: patient})
        .order_by("id")
        .first()
    )


def _prepare(markers, definitions):
    """Validated ``[(time_ms, LabelDefinition)]``, sorted. Raises on anything doubtful."""
    if not isinstance(markers, list):
        raise ValidationError("markers must be a list")
    seen = set()
    out = []
    for index, marker in enumerate(markers):
        if not isinstance(marker, dict):
            raise ValidationError(f"markers[{index}] must be an object")
        time_ms = marker.get("timeMs")
        validate_time_ms(time_ms, where=f"markers[{index}].timeMs")
        if time_ms in seen:
            # Two classifications of one instant is not a state the timeline can draw, and
            # picking one silently would discard the user's later choice.
            raise ValidationError(f"two markers claim {time_ms}ms")
        seen.add(time_ms)
        definition = definitions.get(marker.get("code"))
        if definition is None:
            raise ValidationError(f"markers[{index}] names a quadrant this project does not have")
        out.append((time_ms, definition))
    return sorted(out, key=lambda pair: pair[0])


@transaction.atomic
def save_quadrant_markers(patient, *, source_file, markers, author=None, expected_revision=None):
    """Write one revision holding exactly ``markers`` (``[{"timeMs", "code"}]``)."""
    project = patient.project
    schema = labels.project_label_schema(project, VOCABULARY)
    definitions = {d.code: d for d in schema.definitions.all()}
    prepared = _prepare(markers, definitions)

    annotation_set = get_or_create_set(
        patient,
        KIND,
        annotation_method=labels._method(),
        label_schema=schema,
        created_by=author,
    )
    target = attach_target(
        annotation_set,
        register_file(source_file, content_hash=source_file.file_hash or ""),
        role="video",
    )
    revision = record_revision(
        annotation_set,
        expected_revision=expected_revision,
        author=author,
        origin=AnnotationOrigin.MANUAL,
    )
    for order, (time_ms, definition) in enumerate(prepared):
        add_event(
            revision, target, event_type=EVENT, label=definition, time_ms=time_ms, order=order
        )
    return revision


def quadrant_state(patient):
    """The vocabulary, the revision a save must quote, and the markers."""
    annotation_set = _find_set(patient)
    revision = (
        annotation_set.revisions.order_by("-revision_number").first() if annotation_set else None
    )
    items = (
        revision.eventannotationitems.filter(event_type=EVENT)
        .select_related("label")
        .order_by("time_ms", "id")
        if revision
        else []
    )
    return {
        "revision": revision.revision_number if revision else 0,
        "labels": list_labels(getattr(patient, "project", None)),
        "markers": [
            {"timeMs": item.time_ms, "code": item.label.code} for item in items if item.label
        ],
    }
