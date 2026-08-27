"""The measurement-saving endpoint.

Domain-oriented, per the governing architectural rule: the URL names a patient and the
work being done, and the body is a list of annotations. There is no
``POST /cornerstone/save`` and there is not going to be -- what arrives is translated
into descriptors before anything is written, every number is recomputed from the
geometry, and the viewer's own serialized state is stored as a non-canonical scratch
payload. Replacing Cornerstone would kill the payload and leave the record intact.

Thin, deliberately. It authorises, parses, and hands off to
:func:`annotations.services.viewer.save_measurements`, which is the only thing here
that writes. A view that imported a model and called ``.save()`` would be a review
failure (CONTRIBUTING), and the reason is concrete: every write has to allocate a
revision number against the unique constraint, refresh ``ever_annotated`` and
fingerprint the targets in one transaction, and a caller free to skip a step will
eventually skip the flag.

This lives in ``annotations/`` rather than in a domain app because it serves all three
namespaces and ``AnnotationSet`` already carries all three patient FKs.
``maxillo.views.domain.get_domain_models`` only knows maxillo and laparoscopy, so the
resolution is done here instead of bending that helper.
"""

import json
import logging

from django.apps import apps
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from annotations.services import (
    AnnotationConflict,
    AnnotationNotAllowed,
    current_revision_number,
    save_measurements,
)
from annotations.constants import PayloadFormat, ResourceKind
from annotations.services.viewer import (
    MAX_ANNOTATIONS_PER_REVISION,
    save_measurement_groups,
)
from common.models import FileRegistry
from common.permissions import user_can_write_annotations, user_is_project_admin

logger = logging.getLogger(__name__)

#: URL namespace to the app that owns its ``Patient``.
DOMAIN_APPS = {"maxillo": "maxillo", "brain": "brain", "laparoscopy": "laparoscopy"}


def _namespace(request):
    return (
        getattr(request, "resolver_match", None) and request.resolver_match.namespace
    ) or "maxillo"


def _patient_model(request):
    app_label = DOMAIN_APPS.get(_namespace(request), "maxillo")
    return apps.get_model(app_label, "Patient")


def _file_for_patient(file_id, patient):
    """The requested file, but only if it belongs to this patient.

    Checked explicitly rather than inferred from the patient's read permission: the
    body names a file id and a patient id independently, and without this a user with
    write access to patient A could anchor a measurement set to patient B's volume.
    The annotations would then be fingerprinted against a resource nobody expected,
    and the cross-check would report drift on a scan that never changed.
    """
    file_obj = get_object_or_404(FileRegistry, id=file_id)
    if file_obj.get_patient() != patient:
        return None
    return file_obj


@login_required
@require_POST
def save_measurements_api(request, patient_id):
    """Replace this patient's measurement set with what is currently on screen.

    Body::

        {
          "fileId": 123,
          "fileKey": "volume_nifti",          // optional, for a bundle member
          "expectedRevision": 4,               // the revision the client loaded
          "annotations": [ ...Cornerstone... ],
          "volumeDescriptor": {...},           // optional grid facts
          "coordinateSystem": "patient_lps_mm" // optional; must be a real frame
        }

    Replace-the-whole-set, not a diff: a revision *is* the state of the work at a
    moment, and diffing would need a stable per-annotation identity, which could only
    be the ``annotationUID`` -- the one identifier that is never persisted.
    """
    Patient = _patient_model(request)
    patient = get_object_or_404(Patient, patient_id=patient_id)

    can_write = bool(
        patient.folder and user_can_write_annotations(request.user, patient.folder, request)
    ) or user_is_project_admin(request.user, request)
    if not can_write:
        return JsonResponse({"error": "Permission denied"}, status=403)

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Malformed JSON body"}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "Body must be a JSON object"}, status=400)

    expected_revision = body.get("expectedRevision")
    if expected_revision is not None and (
        not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
    ):
        return JsonResponse(
            {"error": "expectedRevision must be an integer or null"}, status=400
        )

    has_images = "images" in body
    has_single = "fileId" in body or "annotations" in body
    if has_images and has_single:
        # Not a precedence rule. The two shapes name different sets of resources, and
        # the failure being prevented is a viewer that meant to save three images
        # having one of them silently ignored.
        return JsonResponse(
            {
                "error": "send either images or fileId/annotations, not both; "
                "they name different resources and there is no rule for which wins"
            },
            status=400,
        )

    try:
        groups = _groups_from_body(body, patient)
    except _BadRequest as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)

    kwargs = {
        "groups": groups,
        "author": request.user,
        "expected_revision": expected_revision,
        # A photo stack must not take the primary slot from a volume that already has
        # it; a volume grid keeps today's behaviour of claiming it.
        "reclaim_primary": not has_images,
    }
    if body.get("coordinateSystem"):
        kwargs["coordinate_system"] = body["coordinateSystem"]

    try:
        revision = save_measurement_groups(patient, **kwargs)
    except AnnotationConflict as exc:
        # The optimistic-concurrency primitive is the unique constraint on
        # (annotation_set, revision_number). The loser reloads and reapplies; it does
        # not retry, which would just overwrite whoever won.
        return JsonResponse({"error": str(exc), "conflict": True}, status=409)
    except AnnotationNotAllowed as exc:
        return JsonResponse({"error": str(exc)}, status=403)
    except ValidationError as exc:
        # An adapter refusal: an unmapped tool, an incomplete handle set, a NaN
        # coordinate. Nothing was written -- the translation runs before the first row.
        return JsonResponse({"error": _first_message(exc)}, status=400)

    return JsonResponse(
        {
            "revision": revision.revision_number,
            "annotations": sum(len(group["annotations"]) for group in groups),
            "setId": revision.annotation_set_id,
        }
    )


class _BadRequest(Exception):
    """A body the view can name a problem with, and the status it deserves."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _groups_from_body(body, patient):
    """The save's resource groups, from either body shape.

    The legacy shape -- ``fileId`` + ``annotations`` + ``volumeDescriptor`` -- is one
    group holding a *logical volume*, and is validated exactly as it was before. The
    ``images`` shape is N groups holding *files*, which is the honest registration for a
    photograph: a PNG is bytes, not a voxel grid with an affine, and giving them separate
    identity namespaces means the two can never collide on one ``FileRegistry`` row.

    Every group's file is checked against the patient here, before anything is written,
    so one bad group aborts the save rather than writing the others.
    """
    if "images" in body:
        images = body["images"]
        if not isinstance(images, list) or not images:
            raise _BadRequest("images must be a non-empty list")
        groups = []
        seen = set()
        for index, entry in enumerate(images):
            if not isinstance(entry, dict):
                raise _BadRequest(f"images[{index}] must be an object")
            file_obj = _checked_file(entry.get("fileId"), patient, f"images[{index}]")
            key = (file_obj.pk, entry.get("fileKey") or None)
            if key in seen:
                # Two groups for one resource would each be "the state of that image",
                # and the second would silently replace the first within one revision.
                raise _BadRequest(f"images[{index}] names a resource already in this save")
            seen.add(key)
            annotations = entry.get("annotations")
            if not isinstance(annotations, list):
                raise _BadRequest(f"images[{index}].annotations must be a list")
            groups.append(
                {
                    "file_obj": file_obj,
                    "file_key": entry.get("fileKey") or None,
                    "annotations": annotations,
                    "descriptor": entry.get("imageDescriptor") or {},
                    "resource_kind": ResourceKind.FILE,
                    "order": index,
                }
            )
        return groups

    file_obj = _checked_file(body.get("fileId"), patient, "fileId")
    annotations = body.get("annotations")
    if not isinstance(annotations, list):
        raise _BadRequest("annotations must be a list")
    return [
        {
            "file_obj": file_obj,
            "file_key": body.get("fileKey") or None,
            "annotations": annotations,
            "descriptor": body.get("volumeDescriptor") or {},
            "resource_kind": ResourceKind.LOGICAL_VOLUME,
        }
    ]


def _checked_file(file_id, patient, where):
    if not isinstance(file_id, int) or isinstance(file_id, bool):
        raise _BadRequest(
            "fileId must be an integer" if where == "fileId" else f"{where}.fileId must be an integer"
        )
    file_obj = _file_for_patient(file_id, patient)
    if file_obj is None:
        raise _BadRequest("That file does not belong to this patient.", status=403)
    return file_obj


@login_required
def measurements_state_api(request, patient_id):
    """What the viewer should show, and the revision it must quote to save.

    One endpoint for both because a viewer needs both at the same moment: it opens a
    study, draws what is already there, and has to know what to put in
    ``expectedRevision`` when the user saves. Guessing zero means every second editor
    loses a 409 they could have avoided.

    **Only the latest revision is returned.** Revisions are the audit trail and stay in
    the database -- they are what make the raw-data lock defensible and what a
    cross-check reads -- but they are not a concept the viewer exposes. A clinician
    opening a scan sees the measurements that are on it, not a history to navigate.

    The annotations come from the revision's ``cornerstone_state`` payload, which exists
    for exactly this: a non-canonical, editable copy of the viewer's own state, kept so
    a user can resume where they left off. The canonical items are the record; this is
    the resume point. Rebuilding viewer state from the canonical items instead would
    mean inventing handle positions for shapes the model stores as, say, a sphere and a
    radius -- the payload is the honest source.
    """
    Patient = _patient_model(request)
    patient = get_object_or_404(Patient, patient_id=patient_id)

    AnnotationSet = apps.get_model("annotations", "AnnotationSet")
    lookup = {
        "maxillo": "patient",
        "brain": "brain_patient",
        "laparoscopy": "laparoscopy_patient",
    }[DOMAIN_APPS.get(_namespace(request), "maxillo")]
    annotation_set = (
        AnnotationSet.objects.filter(**{lookup: patient, "kind": "measurements"})
        .order_by("id")
        .first()
    )

    if annotation_set is None:
        empty = {
            "revision": 0,
            "setId": None,
            "annotations": [],
            "maxAnnotations": MAX_ANNOTATIONS_PER_REVISION,
        }
        # A caller that asked per-resource gets the per-resource shape back, empty. A
        # client should not have to handle two response shapes depending on whether the
        # patient happens to have been annotated before.
        wanted = request.GET.get("fileIds")
        if wanted is not None:
            ids = _parse_file_ids(wanted)
            if ids is None:
                return JsonResponse(
                    {"error": "fileIds must be a comma-separated list of integers"},
                    status=400,
                )
            empty["images"] = [
                {"fileId": file_id, "fileKey": None, "annotations": []} for file_id in ids
            ]
        return JsonResponse(empty)

    state = _latest_viewer_state(annotation_set)
    payload = {
        "revision": current_revision_number(annotation_set),
        "setId": annotation_set.id,
        "everAnnotated": annotation_set.ever_annotated,
        "maxAnnotations": MAX_ANNOTATIONS_PER_REVISION,
    }

    wanted = request.GET.get("fileIds")
    single = request.GET.get("fileId")
    if wanted is not None:
        ids = _parse_file_ids(wanted)
        if ids is None:
            return JsonResponse({"error": "fileIds must be a comma-separated list of integers"}, status=400)
        payload["images"] = [_group_for(state, file_id) for file_id in ids]
    elif single is not None:
        ids = _parse_file_ids(single)
        if ids is None or len(ids) != 1:
            return JsonResponse({"error": "fileId must be an integer"}, status=400)
        payload["annotations"] = _group_for(state, ids[0])["annotations"]
    else:
        # No narrowing asked for: byte-for-byte what this endpoint returned before it
        # learned about multiple resources.
        payload["annotations"] = state.get("annotations", [])

    return JsonResponse(payload)


def _parse_file_ids(raw):
    """``"1,2,3"`` -> ``[1, 2, 3]``, or ``None`` if any part is not an integer."""
    try:
        return [int(part) for part in raw.split(",") if part.strip() != ""]
    except (TypeError, ValueError):
        return None


def _group_for(state, file_id):
    """One resource's entry from the stored viewer state, empty if it has none.

    Empty rather than absent, and never a fallback to the flat ``annotations`` key: a
    resource with no entry has no measurements on it, and borrowing another resource's
    would draw one image's work on top of a different image.
    """
    for entry in state.get("images", []):
        if isinstance(entry, dict) and entry.get("fileId") == file_id:
            return {
                "fileId": file_id,
                "fileKey": entry.get("fileKey"),
                "annotations": entry.get("annotations") or [],
            }
    return {"fileId": file_id, "fileKey": None, "annotations": []}


def _latest_viewer_state(annotation_set):
    """The viewer state stored on the newest revision, or an empty mapping.

    Empty is the right answer for a set whose latest revision deliberately holds
    nothing -- that is how a deletion is recorded -- so "no payload" and "a payload with
    no annotations" both come back the same way, and neither falls back to an older
    revision. Falling back would resurrect measurements the user deleted.

    Returns the stored shape as it stands: ``annotations`` (written only when the save
    named a single resource) and/or ``images``. Projecting one resource out of it is the
    caller's job, so this function has no opinion about which the client asked for.
    """
    revision = annotation_set.revisions.order_by("-revision_number").first()
    if revision is None:
        return {}
    payload = revision.payloads.filter(format=PayloadFormat.CORNERSTONE_STATE).first()
    data = (payload.data or {}) if payload else {}
    state = {}
    if isinstance(data.get("annotations"), list):
        state["annotations"] = data["annotations"]
    if isinstance(data.get("images"), list):
        state["images"] = data["images"]
    return state


def _first_message(exc):
    messages = getattr(exc, "messages", None)
    return messages[0] if messages else str(exc)
