"""Serving one stored DICOM series to the viewer.

Two endpoints, both read-only, both scoped to the series' own ``FileRegistry`` row.
They are shaped by the client in ``@cornerstonejs/dicom-image-loader@5.8.2`` -- see
``common/dicom/dicomweb.py`` for what each response has to look like and why.

**Authorization is this module's own job.** Finding F9: ``ActiveProfileMiddleware``
only inspects the ``maxillo``/``brain``/``laparoscopy`` path prefixes, and these routes
live under the global ``api`` namespace, which skips it entirely. So every view here
calls :func:`~common.file_access.authorize_file_read` explicitly, against the series'
registry row -- the same single funnel ``serve_file`` uses, resolving the patient
through the row rather than re-deriving it. A new endpoint family that assumed
middleware coverage would be unauthenticated in production and pass every test.

**The demo guest is refused by default.** Finding F10: ``demo_index`` logs an anonymous
visitor in as a real user, so ``@login_required`` alone makes every new endpoint
publicly reachable for ``is_demo`` folders. Native DICOM is exactly the kind of data
that should not become anonymously fetchable by adding a route, so it is gated behind
``settings.DICOM_DEMO_ENABLED``, which ships ``False``.
"""

import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods

from common.demo import is_demo_guest
from common.dicom.dicomweb import (
    content_type_for,
    frame_bytes,
    instance_metadata,
    read_stored_instance,
)
from common.dicom.models import DicomInstance, DicomSeries
from common.file_access import authorize_file_read
from common.object_storage import ObjectStorageError, get_object_storage

logger = logging.getLogger(__name__)


def _resolve_series(request, study_uid, series_uid):
    """The series, once the caller has been shown to be allowed to read it.

    Returns the series or raises ``Http404``. A 404 rather than a 403 for the demo
    guest and for a missing series alike: whether a study exists is itself information.
    """
    if is_demo_guest(request.user) and not getattr(
        settings, "DICOM_DEMO_ENABLED", False
    ):
        raise Http404("Not found")

    series = (
        DicomSeries.objects.select_related("file", "file__patient")
        .filter(series_instance_uid=series_uid, study_instance_uid=study_uid)
        .first()
    )
    if series is None:
        raise Http404("Not found")

    allowed, message, status = authorize_file_read(request.user, series.file)
    if not allowed:
        # Both 403 and 404 collapse to 404 here. The alternative tells an unauthorized
        # caller which series UIDs exist, and a UID is a durable identifier.
        logger.info(
            "DICOM read refused for user %s on series %s: %s (%s)",
            getattr(request.user, "id", None), series_uid, message, status,
        )
        raise Http404("Not found")
    return series


def _fetch(object_key):
    storage = get_object_storage()
    try:
        body, _info = storage.get(object_key)
    except (ObjectStorageError, FileNotFoundError) as exc:
        raise Http404(str(exc)) from exc
    with body:
        return body.read()


@login_required
@require_http_methods(["GET"])
def series_metadata(request, study_uid, series_uid):
    """Every instance in the series, as the DICOM JSON model.

    The loader does **not** fetch this itself: ``metaDataManager.add(imageId, ...)`` is
    called by the application, which is why this is one request for the whole series
    rather than one per instance. On a 400-slice CBCT that is the difference between
    one round trip and four hundred.
    """
    series = _resolve_series(request, study_uid, series_uid)

    documents = []
    for instance in series.instances.all():
        dataset = read_stored_instance(_fetch(instance.object_key))
        documents.append(instance_metadata(dataset))

    # A JSON *array* at the top level, which is what DICOMweb specifies and what the
    # frontend iterates; Django's guard against array responses is not a concern here
    # because nothing is served cross-origin and the payload carries no credentials.
    return JsonResponse(documents, safe=False)


@login_required
@require_http_methods(["GET"])
def instance_frames(request, study_uid, series_uid, sop_uid, frame_numbers):
    """The pixel bytes of one frame.

    ``frame_numbers`` is a comma-separated list in DICOMweb, but the loader always asks
    for exactly one (``imageLoader/wadors/metaDataManager.js`` parses a single integer
    off the end of the URI), so more than one is refused rather than half-answered by
    returning the first.
    """
    series = _resolve_series(request, study_uid, series_uid)

    requested = [part for part in str(frame_numbers).split(",") if part]
    if len(requested) != 1:
        raise Http404("One frame per request")
    try:
        frame_number = int(requested[0])
    except ValueError as exc:
        raise Http404("Frame numbers are integers") from exc

    instance = DicomInstance.objects.filter(
        series=series, sop_instance_uid=sop_uid
    ).first()
    if instance is None:
        raise Http404("Not found")

    dataset = read_stored_instance(_fetch(instance.object_key))
    try:
        payload = frame_bytes(dataset, frame_number)
    except (IndexError, ValueError) as exc:
        raise Http404(str(exc)) from exc

    return HttpResponse(
        payload,
        content_type=content_type_for(
            getattr(getattr(dataset, "file_meta", None), "TransferSyntaxUID", "")
            or series.transfer_syntax_uid
        ),
    )
