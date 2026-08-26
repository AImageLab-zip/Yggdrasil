from dataclasses import dataclass
from typing import BinaryIO, Dict, Generator, Optional, Tuple

from django.http import Http404, StreamingHttpResponse

from .object_storage import ObjectStorageError, get_object_storage


@dataclass(frozen=True)
class ResolvedObject:
    identifier: str
    filename: str
    content_type: Optional[str] = None
    content_length: Optional[int] = None


def _safe_filename(name: str) -> str:
    return (name or "file").replace("\n", " ").replace("\r", " ")


def exists(path_or_key: str) -> bool:
    if not path_or_key:
        return False
    storage = get_object_storage()
    try:
        return storage.exists(path_or_key)
    except ObjectStorageError:
        return False


def open_binary(path_or_key: str) -> Tuple[BinaryIO, ResolvedObject]:
    if not path_or_key:
        raise FileNotFoundError("empty")

    storage = get_object_storage()
    body, info = storage.get(path_or_key)
    filename = path_or_key.rstrip("/").split("/")[-1] or "file"
    return body, ResolvedObject(
        identifier=path_or_key,
        filename=_safe_filename(filename),
        content_type=info.content_type,
        content_length=info.content_length,
    )


def iter_bytes(
    path_or_key: str, *, chunk_size: int = 1024 * 1024
) -> Generator[bytes, None, None]:
    storage = get_object_storage()
    yield from storage.iter_bytes(path_or_key, chunk_size=chunk_size)


def streaming_response(
    *,
    path_or_key: str,
    content_type: str,
    filename: str,
    as_attachment: bool = False,
    extra_headers: Optional[Dict[str, str]] = None,
) -> StreamingHttpResponse:
    if not path_or_key:
        raise Http404("File not found")

    try:
        response = StreamingHttpResponse(
            iter_bytes(path_or_key), content_type=content_type
        )
    except FileNotFoundError:
        raise Http404("File not found")
    except ObjectStorageError as exc:
        raise Http404(str(exc))

    disp = "attachment" if as_attachment else "inline"
    safe_name = _safe_filename(filename)
    response["Content-Disposition"] = f'{disp}; filename="{safe_name}"'

    if extra_headers:
        for k, v in extra_headers.items():
            response[str(k)] = str(v)

    return response


def authorize_file_read(user, file_obj, namespace=None):
    """Authorize ``user`` to read ``file_obj``, scoped to the file's own domain.

    Returns ``(allowed, error_message, status_code)``; on success the last two
    are ``None``.

    This is the single authorization funnel for every endpoint that streams a
    ``FileRegistry`` row. It exists because the per-domain copies had drifted:
    the maxillo copy resolved the patient with an ``if laparoscopy / else
    .patient`` branch (so a brain row consulted the maxillo FK) and then
    authorized every domain against a hardcoded ``slug='maxillo'`` project, so
    brain and laparoscopy files were gated on maxillo project membership in
    both directions -- granting access to maxillo members who had none, and
    denying it to laparoscopy-only members who did.

    Authorization resolves the patient through the domain registry and defers
    to ``patient.project``, which is mandatory on all three Patient models.
    """
    from common.domains import fk_fields_for, normalize_domain
    from common.models import ProjectAccess
    from common.modality_config import raw_file_hidden
    from common.permissions import user_can_read_patient, user_can_view_caption_content

    if file_obj is None:
        return False, "File not found", 404

    # The row's own domain wins; the request namespace is only a fallback for
    # legacy rows that predate the column (and for the global "api" namespace,
    # which is not a domain).
    file_domain = normalize_domain(file_obj.domain or namespace)
    patient_fk, caption_fk = fk_fields_for(file_domain)

    patient = getattr(file_obj, patient_fk, None)
    if patient is None:
        # Tolerate mis-filed rows: fall back across the other domains' FKs
        # rather than 403-ing on data the uploader wrote to the wrong column.
        for other_domain in ("maxillo", "brain", "laparoscopy"):
            other_fk, _ = fk_fields_for(other_domain)
            patient = getattr(file_obj, other_fk, None)
            if patient is not None:
                break

    if patient is not None:
        if getattr(patient, "deleted", False):
            return False, "Patient not found", 404

        # Resolves patient.project internally -- never a hardcoded domain.
        if not user_can_read_patient(user, patient):
            return False, "Permission denied", 403

        caption = getattr(file_obj, caption_fk, None)
        if caption is None:
            for other_domain in ("maxillo", "brain", "laparoscopy"):
                _, other_caption_fk = fk_fields_for(other_domain)
                caption = getattr(file_obj, other_caption_fk, None)
                if caption is not None:
                    break
        # A caption file needs the caption gate too (annotators do not see
        # other annotators' captions), on top of the patient read above.
        if caption is not None and not user_can_view_caption_content(
            user, caption, file_domain
        ):
            return False, "Permission denied", 403
    else:
        # Orphaned row: no patient to scope against, so require admin anywhere.
        if not (user and user.is_authenticated):
            return False, "Permission denied", 403
        if not user.is_staff and not ProjectAccess.objects.filter(
            user=user, role="admin"
        ).exists():
            return False, "Permission denied", 403

    # Backstop: a raw input that is discarded, or blocked until its processing
    # completes, must never be served even via a direct URL.
    if raw_file_hidden(file_obj):
        return False, "File not found", 404

    return True, None, None
