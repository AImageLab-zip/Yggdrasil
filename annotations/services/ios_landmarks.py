"""Saving and reading IOS dental landmarks, through the same machinery measurements use.

Thin on purpose, and for the same reasons :mod:`annotations.services.segmentation` is.
Everything structural -- one revision spanning both jaws, carrying forward the jaw a save
did not name, the optimistic-concurrency check, the monotonic lock -- is
:func:`annotations.services.viewer.save_measurement_groups`. This module is what tells it
that the items are landmark points rather than measurements.

Four differences from a measurement save:

**Its own set kind.** ``ios_landmarks`` was already an ``AnnotationSet.kind`` and already
what ``annotations_materialize_landmarks`` writes, so reusing it is what lets
``annotations_crosscheck`` find both representations of one study.

**Labels are required.** An FDI code decides which tooth a landmark is exported under, so
a point written unlabelled is a point exported against the wrong tooth, looking fine doing
it. ``apply_descriptors`` refuses rather than defaults.

**No scratch payload.** A landmark *is* a point. The items are the whole truth, and a
second copy allowed to go stale would only ever disagree with them.

**The mesh is named, and the model insists.** Coordinates are ``resource_local`` -- one
STL's own object space -- so ``add_spatial_3d`` refuses to write them without a resolved
target resource. That is why the write path takes a ``file_obj`` per jaw rather than a
patient and a document: there is no way to file these points without saying which geometry
they are points *on*. Closing that gap is the whole reason Phase 6 touches storage.
"""

from django.core.exceptions import ValidationError

from annotations.adapters.ios_landmarks import (
    LANDMARKS_KIND,
    LANDMARK_KEY_RE,
    ios_landmarks,
    jaw_for_tooth,
    landmark_key,
    landmarks_from_items,
)
from annotations.constants import AnnotationOrigin, CoordinateSystem, ResourceKind
from annotations.services.labels import fdi_schema
from annotations.services.viewer import save_measurement_groups
from common.models import AnnotationMethod

#: The ``AnnotationMethod`` slug that gates this work, and the one
#: ``annotations_materialize_landmarks`` already resolves, so converted and live work are
#: gated identically.
LANDMARKS_METHOD_SLUG = "ios_landmarks"

#: The two arches, in the order a save reports them.
JAWS = ("upper", "lower")

#: The target role each jaw anchors under.
#:
#: One role per jaw rather than a shared ``"mesh"``, so the target row says which arch it
#: is without a reader having to re-resolve today's scan pair to interpret its own rows --
#: and so a re-scan gets its own target while the superseded mesh keeps the landmarks that
#: were placed on it. ``attach_target`` keys on ``(annotation_set, source_resource, role)``,
#: so these strings are chosen once and never varied: Phase 5 shipped a latent defect of
#: exactly that shape, where the live path used ``role=""`` and the converter ``"image"``,
#: and a converted study edited live read back doubled.
MESH_ROLES = {"upper": "mesh_upper", "lower": "mesh_lower"}

#: The role ``annotations_materialize_landmarks`` anchors converted documents under.
#:
#: Read-only history. The legacy artifact names the patient and not the mesh, so the
#: converter targets the JSON file itself and says so. The reader below never looks at a
#: target role -- it groups by the jaw the FDI code implies -- which is what lets a
#: converted study be read and then re-anchored by its first live save, with no backfill
#: command and no second read path.
LANDMARK_DOCUMENT_ROLE = "landmark_document"


def landmark_method():
    """The ``AnnotationMethod`` row, or ``None`` if the registry has no such entry.

    ``None`` means "ungated", which is what ``get_or_create_set`` already does with it --
    the right answer for a deployment that has not registered the method, rather than a
    reason to refuse every save.
    """
    return AnnotationMethod.objects.filter(slug=LANDMARKS_METHOD_SLUG).first()


def _document_for(patient_id, jaw, landmarks, *, where):
    """One jaw's ``{FDI: entry}`` map as the legacy-keyed document the adapter reads.

    The wire format is FDI-keyed and jaw-scoped because the legacy key repeats two facts
    the request already carries -- the patient, and an arch the FDI code determines. Asking
    a client to restate them is asking it to contradict them.
    """
    if not isinstance(landmarks, dict):
        raise ValidationError(f"{where}: landmarks must be an object keyed by FDI code")
    document = {}
    for code, entry in landmarks.items():
        tooth = str(code)
        # The jaw/FDI consistency the legacy server enforced on every save, kept because
        # a point filed under the wrong arch is a point drawn on the wrong mesh.
        if jaw_for_tooth(tooth) != jaw:
            raise ValidationError(
                f"{where}: tooth {tooth} is not in the {jaw} jaw"
            )
        document[landmark_key(patient_id, tooth)] = entry
    return document


def save_ios_landmarks(
    patient,
    *,
    meshes,
    author=None,
    expected_revision=None,
    annotation_method=None,
    note="",
    origin=AnnotationOrigin.MANUAL,
):
    """Write one revision holding the landmarks on every mesh named.

    :param meshes: ``[{"file_obj": <FileRegistry>, "jaw": "upper"|"lower",
        "landmarks": {FDI: {type: [x, y, z] | [[x, y, z], ...]}}, "descriptor": {...}}]``.
        A mesh with an empty ``landmarks`` map is how a deletion is expressed -- omitting
        it would have the server carry the old points forward. A jaw the save does not
        mention keeps exactly what it had.
    :param expected_revision: the revision the client loaded; stale raises
        :class:`~annotations.services.exceptions.AnnotationConflict` -> 409. The legacy
        whole-document ``PUT`` had no such check, so two annotators on one patient
        silently clobbered each other.
    :param origin: ``PREDICTION`` for the landmark job's output, which is what keeps model
        output from setting the monotonic ``ever_annotated`` flag.
    :returns: the new ``AnnotationRevision``.
    """
    meshes = list(meshes or [])
    if not meshes:
        raise ValidationError("a landmark save must name at least one mesh")

    patient_id = patient.patient_id
    domain_field = _domain_field(patient)
    groups = []
    seen = set()
    for index, mesh in enumerate(meshes):
        where = f"meshes[{index}]"
        jaw = mesh.get("jaw")
        if jaw not in MESH_ROLES:
            raise ValidationError(f"{where}: jaw must be one of {list(MESH_ROLES)}")
        if jaw in seen:
            raise ValidationError(f"{where}: the {jaw} jaw is named twice")
        seen.add(jaw)
        if mesh.get("file_obj") is None:
            raise ValidationError(f"{where}: a mesh file is required")

        groups.append(
            {
                "file_obj": mesh["file_obj"],
                "file_key": None,
                # Carried so the shared writer can count and validate uniformly; the
                # translation below is what actually reads the landmarks.
                "annotations": [],
                "document": _document_for(
                    patient_id, jaw, mesh.get("landmarks"), where=where
                ),
                "descriptor": mesh.get("descriptor") or {},
                "resource_kind": ResourceKind.FILE,
                "role": MESH_ROLES[jaw],
                "order": JAWS.index(jaw),
            }
        )

    legacy = _legacy_anchor_group(patient, domain_field, named_jaws=seen)
    if legacy is not None:
        groups.append(legacy)

    return save_measurement_groups(
        patient,
        groups=groups,
        author=author,
        expected_revision=expected_revision,
        coordinate_system=CoordinateSystem.RESOURCE_LOCAL,
        annotation_method=annotation_method or landmark_method(),
        note=note,
        origin=origin,
        kind=LANDMARKS_KIND,
        label_schema=fdi_schema(),
        require_labels=True,
        translate=lambda group: ios_landmarks(group["document"], patient_id=patient_id),
        store_payload=False,
        # The upper jaw claims the slot on the first save and keeps it thereafter; a
        # patient owns meshes, photographs and a volume, so the slot answers "what is this
        # set mostly about" rather than "what was saved last".
        reclaim_primary=False,
        primary_index=0,
    )


#: ``AnnotationSet``'s patient FK for each domain. IOS is maxillo-only today, but
#: resolving it from the model rather than hardcoding ``patient`` is what keeps this from
#: being the line that breaks when a second domain grows meshes.
_DOMAIN_FIELDS = {"maxillo": "patient", "brain": "brain_patient"}


def _domain_field(patient):
    return _DOMAIN_FIELDS.get(patient._meta.app_label, "laparoscopy_patient")


def _legacy_anchor_group(patient, domain_field, *, named_jaws):
    """The group that retires a converted study's ``landmark_document`` anchor.

    ``annotations_materialize_landmarks`` anchors a converted document to the JSON file,
    because the legacy artifact named the patient and never the mesh. A live save writes
    *mesh* targets instead -- so without this, the old target is one the save did not name,
    ``save_measurement_groups`` carries its items forward, and the study ends up holding
    both copies. Single-point types would survive that (the same value written twice), but
    ``cusps`` and ``planar`` are lists the reader appends to, so they would silently
    double on the first edit of every converted study.

    Naming the target here is what stops that. It is handed exactly the jaws **this save
    did not mention**, so:

    - a save naming both arches empties the old anchor and the set is fully re-anchored;
    - a save naming one arch moves that arch and leaves the other where it was, rather
      than deleting landmarks the client never sent and could not have known to send.

    Returns ``None`` for a set that was never converted, which is every set created after
    Phase 6 -- so this is migration machinery with a finite life, not a second write path.
    """
    from annotations.models import AnnotationSet

    annotation_set = (
        AnnotationSet.objects.filter(**{domain_field: patient, "kind": LANDMARKS_KIND})
        .order_by("id")
        .first()
    )
    if annotation_set is None:
        return None
    target = (
        annotation_set.targets.filter(role=LANDMARK_DOCUMENT_ROLE)
        .select_related("source_resource")
        .first()
    )
    if target is None or target.source_resource.file is None:
        return None

    state = ios_landmarks_state(patient, domain_field=domain_field)
    remaining = {
        key: entry
        for key, entry in state["document"].items()
        if (match := LANDMARK_KEY_RE.match(key)) and match.group(2) not in named_jaws
    }
    return {
        "file_obj": target.source_resource.file,
        "file_key": None,
        "annotations": [],
        "document": remaining,
        "descriptor": {},
        "resource_kind": ResourceKind.FILE,
        "role": LANDMARK_DOCUMENT_ROLE,
        "order": len(JAWS),
    }


def ios_landmarks_state(patient, *, domain_field):
    """The latest landmarks, and the revision a save must quote.

    Rebuilt from the canonical items rather than a payload -- see the module note. Only the
    latest revision is read: revisions stay in the database as the audit trail, but they
    are not a concept the editor exposes, and falling back to an older one would resurrect
    points somebody deleted.

    **Items are grouped by the jaw their FDI code implies, not by their target's role.**
    That is what lets one read path serve both anchors: a study converted by
    ``annotations_materialize_landmarks`` carries a ``landmark_document`` target naming the
    JSON file, and a study saved by this module carries one target per mesh. The arch is
    the same fact in both, it is the fact the editor needs, and the write path enforces it
    -- so reading it from the code rather than the target means a converted study reads
    correctly today and is re-anchored by its first live save, with no backfill.

    :returns: ``{"revision": int, "setId": int|None, "everAnnotated": bool,
        "document": {legacy_key: entry}, "jaws": {"upper": {FDI: entry}, "lower": {...}},
        "updatedAt": datetime|None}``
    """
    from annotations.models import AnnotationSet, SpatialAnnotation3DItem

    annotation_set = (
        AnnotationSet.objects.filter(**{domain_field: patient, "kind": LANDMARKS_KIND})
        .order_by("id")
        .first()
    )
    empty = {
        "revision": 0,
        "setId": None,
        "everAnnotated": False,
        "document": {},
        "jaws": {jaw: {} for jaw in JAWS},
        "updatedAt": None,
    }
    if annotation_set is None:
        return empty

    revision = annotation_set.revisions.order_by("-revision_number").first()
    if revision is None:
        return {
            **empty,
            "setId": annotation_set.id,
            "everAnnotated": annotation_set.ever_annotated,
            "updatedAt": annotation_set.updated_at,
        }

    items = (
        SpatialAnnotation3DItem.objects.filter(revision=revision)
        .select_related("label")
        .order_by("order", "id")
    )
    document = landmarks_from_items(items, patient_id=patient.patient_id)

    jaws = {jaw: {} for jaw in JAWS}
    for key, entry in document.items():
        match = LANDMARK_KEY_RE.match(key)
        if match:
            jaws[match.group(2)][match.group(3)] = entry

    return {
        "revision": revision.revision_number,
        "setId": annotation_set.id,
        "everAnnotated": annotation_set.ever_annotated,
        "document": document,
        "jaws": jaws,
        "updatedAt": annotation_set.updated_at,
    }
