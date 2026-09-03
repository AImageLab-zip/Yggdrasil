"""Service-layer failures, each mapping to one HTTP status.

Views in this repository are plain functions returning ``JsonResponse``; there
is no DRF exception handler to lean on. So the service says what kind of failure
it is and the view translates, rather than the service building a response (it
would then have to know it was being called from HTTP) or the view guessing from
an exception message.
"""


class AnnotationServiceError(Exception):
    """Base class. ``status_code`` is what a view should return."""

    status_code = 400


class AnnotationConflict(AnnotationServiceError):
    """Somebody else wrote first.

    Raised when the revision number a write claimed is already taken -- the
    ``IntegrityError`` from ``UniqueConstraint(annotation_set, revision_number)``,
    translated. The client's copy is stale; it has to re-read and re-apply.
    """

    status_code = 409


class AnnotationLocked(AnnotationServiceError):
    """The work is frozen and this write would change it.

    Distinct from a permission failure: the user may have every right to edit
    and the answer is still no, because the record has to stay explicable.
    """

    status_code = 409


class AnnotationNotAllowed(AnnotationServiceError):
    """The project does not enable this annotation method."""

    status_code = 403
