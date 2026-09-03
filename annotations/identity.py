"""Construct ``SourceResource.identity_key``. Pure functions, no database.

The column is unique and unconditional (see ``annotations.models.resources`` for
why), which makes it only as trustworthy as the string that goes into it. Two
callers spelling the same volume differently would create two resources for one
set of bytes and split its annotations across both, so construction lives here,
in one place, with tests.

The format is ``<kind>:<parts joined by '/'>``. It is a database key, not a URL:
it is never parsed by a client, never routed, and must stay stable when routes
change. Nothing in it is derived from a Cornerstone id.
"""

from annotations.constants import ResourceKind

#: Separator between the kind prefix and the body, and between body parts.
_KIND_SEPARATOR = ":"
_PART_SEPARATOR = "/"

#: ``SourceResource.identity_key`` is ``varchar(255)``. Building a longer string
#: would be truncated by MySQL in non-strict mode -- silently colliding two
#: resources -- so it is an error here instead.
MAX_IDENTITY_KEY_LENGTH = 255


class IdentityError(ValueError):
    """An identity key could not be built from the values given."""


def _clean(part, *, what):
    """Reject anything that would make the key ambiguous or unstable."""
    if part is None:
        raise IdentityError(f"{what} is required")
    text = str(part).strip()
    if not text:
        raise IdentityError(f"{what} is required")
    if _KIND_SEPARATOR in text or _PART_SEPARATOR in text:
        raise IdentityError(
            f"{what} may not contain {_KIND_SEPARATOR!r} or {_PART_SEPARATOR!r}: {text!r}"
        )
    return text


def _build(kind, *parts):
    if kind not in ResourceKind.ALL:
        raise IdentityError(f"unknown resource kind {kind!r}")
    key = kind + _KIND_SEPARATOR + _PART_SEPARATOR.join(parts)
    if len(key) > MAX_IDENTITY_KEY_LENGTH:
        raise IdentityError(
            f"identity key is {len(key)} characters, over the "
            f"{MAX_IDENTITY_KEY_LENGTH}-character column limit: {key!r}"
        )
    return key


def for_file(file_id, file_key=None):
    """Identity of a ``FileRegistry`` row, or of one member of its bundle.

    A bundle member gets its own resource rather than sharing the bundle's,
    because the volume and the segmentation inside a ``cbct_processed`` artifact
    are different content with different annotations. ``file_key=None`` and
    ``file_key='primary'`` are the same thing -- the row's own ``file_path`` --
    and must produce the same key, since ``maxillo.api_views.files`` already
    treats a missing key as ``primary``.
    """
    identifier = _clean(file_id, what="file id")
    key = (file_key or "primary").strip() or "primary"
    if key == "primary":
        return _build(ResourceKind.FILE, identifier)
    return _build(ResourceKind.FILE, identifier, _clean(key, what="file key"))


def for_logical_volume(file_id, file_key=None):
    """Identity of the *volume* a user annotates, which may live in a bundle.

    Distinct from :func:`for_file` on purpose. The same bytes can be addressed
    as a file (for serving, export, hashing) and as a volume (for annotation),
    and only the second one carries the promise that its voxel grid and affine
    are what the stored coordinates were measured against.
    """
    identifier = _clean(file_id, what="file id")
    key = (file_key or "primary").strip() or "primary"
    if key == "primary":
        return _build(ResourceKind.LOGICAL_VOLUME, identifier)
    return _build(ResourceKind.LOGICAL_VOLUME, identifier, _clean(key, what="file key"))


def for_derived_resource(producer, source_identity_key, discriminator=None):
    """Identity of something computed from another resource.

    ``source_identity_key`` is another resource's key, so a derived resource
    records what it came from in its own name -- a panoramic strip is only
    interpretable together with the volume and arch it was baked from. The
    embedded key contains separators, so it is the last part and is not cleaned
    the way the others are; it is already a validated key by construction.
    """
    parts = [_clean(producer, what="producer")]
    if discriminator is not None:
        parts.append(_clean(discriminator, what="discriminator"))
    source = str(source_identity_key or "").strip()
    if not source:
        raise IdentityError("source identity key is required")
    parts.append(source)
    return _build(ResourceKind.DERIVED_RESOURCE, *parts)
