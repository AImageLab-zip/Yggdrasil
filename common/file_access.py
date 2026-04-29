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
