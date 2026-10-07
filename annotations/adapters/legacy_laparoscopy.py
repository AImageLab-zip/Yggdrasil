"""Translating laparoscopy's one remaining legacy annotation table, ``Classification``.

Pure: legacy values in, descriptor dicts out.
"""

from annotations.adapters import descriptors


def classification_notes(*, notes, classifier="manual"):
    """Convert a laparoscopy ``Classification`` row into an event descriptor.

    Unlike maxillo's, this table holds no occlusion facets -- only free text.
    So it converts to a single note event rather than five facet events, and it
    keeps its own set kind: calling it an occlusion classification because the
    two share a table name would put a surgeon's remark under a dental heading.
    """
    text = (notes or "").strip()
    return [
        descriptors.event(
            event_type="study_notes",
            value=text,
            attributes={"classifier": classifier, "has_notes": bool(text)},
        )
    ]
