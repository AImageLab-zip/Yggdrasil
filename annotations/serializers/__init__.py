"""Serializing annotation work to and from Yggdrasil's own JSON.

Domain-oriented, not viewer-oriented: the document describes annotations, not a
viewer's tool state, and it carries no Cornerstone runtime identifier. There is
no ``POST /cornerstone/save`` at the other end of this and there is not going to
be -- a viewer's serialized state is stored as an editable payload alongside the
canonical document, never as the canonical document.
"""

from annotations.serializers.documents import (
    DOCUMENT_VERSION,
    assert_no_viewer_identifiers,
    build_document,
    document_summary,
)

__all__ = [
    "DOCUMENT_VERSION",
    "assert_no_viewer_identifiers",
    "build_document",
    "document_summary",
]
