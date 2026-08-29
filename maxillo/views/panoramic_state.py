"""The current browser-generated panoramic for a patient: one reader, one answer.

Three view modules used to ask this question and each answered it for itself --
``patient_detail._panorex_source_data`` built the editor's payload, ``patient_data``
decided whether to serve the strips, and the save endpoint decided what revision a client
had to quote. All three compared the same seven source fields by hand, and all three would
have had to change together.

They now ask :func:`current_browser_panoramic`, and the comparison is one line of it.

**Where the arch lives.** In ``annotations`` (decision #20), like every other sparse
annotation: ``annotations.services.panoramic`` writes it and
``annotations.services.panoramic_arch_state`` reads it back.  ``maxillo.PanoramicState`` is
frozen history for the cross-check release and is read here only to answer "has this
patient ever had one", which has to be true for a study whose conversion has not run yet.

**How "the source changed" is expressed.** Not by deleting anything. Every revision is
stamped with ``source_fingerprint`` -- ``{identity_key: content_hash}`` over its targets --
so replacing a CBCT's bytes makes the stored arch stop matching, all by itself. That is why
``metadata.update_nifti_metadata`` needs no annotation-side cleanup: rewriting an affine
changes the file hash, and the arch is thereby known to describe bytes that no longer
exist.
"""

from annotations import services as annotation_services

#: The baker whose output this module understands. An arch produced by a superseded
#: algorithm is history, not something to serve as the current panoramic.
BROWSER_PANORAMIC_ALGORITHM = "panorex-js-v2-mip"


def current_browser_panoramic(patient, source):
    """The arch this patient has, its strips, and whether they describe *this* CBCT.

    ``revision`` is the server's own count, and is what a *write* must quote.
    ``effectiveRevision`` is what the **client** must quote: zero when the arch no longer
    describes the active source, which is how "the CBCT was replaced, start again" is
    expressed without rewinding a revision number the database keeps monotonic.

    :param patient: a maxillo ``Patient``.
    :param source: ``_resolved_cbct_viewer_source(patient)``, possibly ``None``.
    :returns: ``{"revision", "effectiveRevision", "arch", "strips", "defaultMode",
        "generationUuid", "requestHash", "matchesSource"}``. ``arch`` is ``None`` for a
        patient who has never had one; ``strips`` maps ``"mip"``/``"raysum"`` to a
        ``FileRegistry`` row.
    """
    state = annotation_services.panoramic_arch_state(patient)
    arch = state["arch"]
    strips = state["strips"]

    matches = (
        bool(source)
        and (arch or {}).get("algorithm_version") == BROWSER_PANORAMIC_ALGORITHM
        and annotation_services.arch_describes_source(
            state,
            volume_file=source["file"],
            volume_file_key=source["file_key"],
            volume_hash=source["file_hash"],
            segmentation_file=source["segmentation_file"],
            segmentation_file_key=source["segmentation_key"],
            segmentation_hash=source["segmentation_hash"],
        )
    )

    # Both strips carry the same upload bookkeeping; either will do. A revision holding
    # only one of them is a half-written state the save transaction makes impossible.
    marker = strips.get("mip") or strips.get("raysum")
    metadata = marker.metadata if marker is not None and isinstance(marker.metadata, dict) else {}

    return {
        "revision": state["revision"],
        "effectiveRevision": state["revision"] if matches else 0,
        "arch": arch,
        "strips": strips,
        "defaultMode": (arch or {}).get("default_mode") or "mip",
        "generationUuid": metadata.get("generation_uuid"),
        "requestHash": metadata.get("request_hash"),
        "matchesSource": matches,
    }


def has_browser_panoramic(patient):
    """Whether a panoramic has ever been produced for this patient, from either store.

    The union matters for the cross-check release. ``PanoramicState`` is frozen history
    now, but a study whose conversion has not run yet has its arch *only* there -- and
    reading only ``annotations`` would offer that patient the one free silent default a
    second time, on a case whose raw data is already locked.
    """
    # Imported here: the model module imports view helpers transitively.
    from maxillo.models import PanoramicState

    if annotation_services.panoramic_arch_state(patient)["arch"] is not None:
        return True
    return PanoramicState.objects.filter(patient=patient).exists()
