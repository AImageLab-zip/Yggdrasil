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


#: The only content types a stored file is ever served under inline: things a
#: browser displays without executing. Anything else -- HTML, SVG, XML, scripts, or a
#: type guessed from an uploader-chosen extension -- goes out as an
#: ``application/octet-stream`` attachment, so an uploaded ``.html`` or ``.svg``
#: cannot run as whoever opens it on this origin.
INLINE_CONTENT_TYPES = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/tiff",
    "video/mp4", "video/webm", "video/quicktime",
    "audio/webm", "audio/mpeg", "audio/wav", "audio/x-wav", "audio/ogg", "audio/mp4",
    "application/json",
})

#: Sent with every stored-file response: even if a browser were talked into
#: rendering one as a document, it gets no script, no plugins and a unique origin.
FILE_RESPONSE_CSP = "default-src 'none'; sandbox"


def served_content_type(content_type: Optional[str], as_attachment: bool = False) -> Tuple[str, bool]:
    """``(content_type, as_attachment)`` a stored file may actually be served with."""
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if base in INLINE_CONTENT_TYPES:
        return base, as_attachment
    return "application/octet-stream", True


def _safe_filename(name: str) -> str:
    """Filename with control characters removed (the header-injection guard)."""
    return "".join(ch if ch.isprintable() else " " for ch in (name or "file")) or "file"


def content_disposition(filename: str, as_attachment: bool) -> str:
    """RFC 6266 header: an ASCII fallback plus the UTF-8 name (RFC 5987)."""
    from urllib.parse import quote

    name = _safe_filename(filename)
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace("\\", "_").replace('"', "_")
    disp = "attachment" if as_attachment else "inline"
    return f"{disp}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def apply_file_headers(response, *, content_type: Optional[str], filename: str, as_attachment: bool = False):
    """Content type, disposition and sandbox headers for a stored-file response."""
    served_type, attachment = served_content_type(content_type, as_attachment)
    response["Content-Type"] = served_type
    response["Content-Disposition"] = content_disposition(filename, attachment)
    response["Content-Security-Policy"] = FILE_RESPONSE_CSP
    response["X-Content-Type-Options"] = "nosniff"
    return response


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
        response = StreamingHttpResponse(iter_bytes(path_or_key))
    except FileNotFoundError:
        raise Http404("File not found")
    except ObjectStorageError as exc:
        raise Http404(str(exc))

    apply_file_headers(
        response, content_type=content_type, filename=filename, as_attachment=as_attachment
    )

    if extra_headers:
        for k, v in extra_headers.items():
            response[str(k)] = str(v)

    return response


def parse_byte_range(header: str, total_size: int) -> Optional[Tuple[int, int]]:
    """``(start, end)`` inclusive for a single ``bytes=`` range, or ``None``.

    ``None`` means "serve the whole file": no header, a multi-range or malformed
    header, or a range that does not lie inside the file.
    """
    import re

    m = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not m or total_size <= 0 or not (m.group(1) or m.group(2)):
        return None
    if m.group(1):
        start = int(m.group(1))
        end = min(int(m.group(2)), total_size - 1) if m.group(2) else total_size - 1
    else:  # suffix range: the last N bytes
        start = max(total_size - int(m.group(2)), 0)
        end = total_size - 1
    if start > end or start >= total_size:
        return None
    return start, end


def range_response(
    *, path_or_key: str, content_type: str, filename: str, byte_range: Tuple[int, int], total_size: int
) -> StreamingHttpResponse:
    """A 206 for ``byte_range`` of a stored object (media seeking)."""
    start, end = byte_range
    body, _ = get_object_storage().get_range(path_or_key, f"bytes={start}-{end}")

    def _chunks(chunk_size=512 * 1024):
        try:
            while True:
                data = body.read(chunk_size)
                if not data:
                    break
                yield data
        finally:
            body.close()

    response = StreamingHttpResponse(_chunks(), status=206)
    apply_file_headers(response, content_type=content_type, filename=filename)
    response["Content-Range"] = f"bytes {start}-{end}/{total_size}"
    response["Content-Length"] = str(end - start + 1)
    response["Accept-Ranges"] = "bytes"
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
    from common.domains import DOMAINS, fk_fields_for, normalize_domain
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
        for other_domain in DOMAINS:
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
            for other_domain in DOMAINS:
                _, other_caption_fk = fk_fields_for(other_domain)
                caption = getattr(file_obj, other_caption_fk, None)
                if caption is not None:
                    break
        # A caption file needs the caption gate too (annotators do not see
        # other annotators' captions), on top of the patient read above.
        if caption is not None and not user_can_view_caption_content(user, caption):
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
