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
from annotations.services.viewer import MAX_ANNOTATIONS_PER_REVISION
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

    file_id = body.get("fileId")
    if not isinstance(file_id, int) or isinstance(file_id, bool):
        return JsonResponse({"error": "fileId must be an integer"}, status=400)

    file_obj = _file_for_patient(file_id, patient)
    if file_obj is None:
        return JsonResponse(
            {"error": "That file does not belong to this patient."}, status=403
        )

    annotations = body.get("annotations")
    if not isinstance(annotations, list):
        return JsonResponse({"error": "annotations must be a list"}, status=400)

    expected_revision = body.get("expectedRevision")
    if expected_revision is not None and (
        not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
    ):
        return JsonResponse(
            {"error": "expectedRevision must be an integer or null"}, status=400
        )

    kwargs = {
        "file_obj": file_obj,
        "file_key": body.get("fileKey") or None,
        "annotations": annotations,
        "author": request.user,
        "expected_revision": expected_revision,
        "volume_descriptor": body.get("volumeDescriptor") or {},
    }
    if body.get("coordinateSystem"):
        kwargs["coordinate_system"] = body["coordinateSystem"]

    try:
        revision = save_measurements(patient, **kwargs)
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
            "annotations": len(annotations),
            "setId": revision.annotation_set_id,
        }
    )


@login_required
def measurements_state_api(request, patient_id):
    """The revision number a client must quote to save, and how much is stored.

    Read-only. Exists so a viewer opening a study knows what to put in
    ``expectedRevision`` without guessing zero -- guessing zero means every second
    editor loses a 409 they could have avoided.
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
        return JsonResponse({"revision": 0, "setId": None, "maxAnnotations": MAX_ANNOTATIONS_PER_REVISION})

    return JsonResponse(
        {
            "revision": current_revision_number(annotation_set),
            "setId": annotation_set.id,
            "everAnnotated": annotation_set.ever_annotated,
            "maxAnnotations": MAX_ANNOTATIONS_PER_REVISION,
        }
    )


def _first_message(exc):
    messages = getattr(exc, "messages", None)
    return messages[0] if messages else str(exc)
