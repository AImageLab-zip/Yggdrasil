"""Which pair of IOS scan meshes a patient's landmarks belong to.

One function, because there are now four callers -- the ``/data/`` endpoint the viewer
loads from, the landmark save endpoint, the landmark state reader and the prediction
writer -- and they must all agree. A landmark is stored in ``resource_local`` coordinates,
which is one mesh's own object space, so two callers disagreeing about which STL is
"the upper scan" is two callers disagreeing about what the numbers mean.

**The policy is a database flag, and that has a consequence worth stating.** Whether the
viewer serves the raw or the processed (re-oriented) mesh is
``ProcessingStep.prefer_processed_for_viewer``, editable in admin. Raw and processed are
different geometry, so flipping it re-frames every landmark already stored against the
other one. Recording the mesh on every save -- which is what Phase 6 does -- makes that
recoverable from here on. It does not repair the pre-Phase-6 corpus, where the artifact
named the patient and never the mesh, and nothing can: which STL those points were picked
against was never written down.
"""

#: The arches, upper first, which is the order the viewer and the save report them in.
JAWS = ("upper", "lower")


def current_ios_pair(patient):
    """The upper/lower mesh rows the viewer would show this patient today.

    A *complete* pair or nothing: a jaw on its own is not something the IOS viewer can
    render, and half a pair would anchor landmarks against a mesh the user never saw.

    Raw candidates are dropped when the raw file is hidden, matching what the viewer will
    actually serve -- offering an anchor the file endpoint would refuse to hand over is a
    save that succeeds and then cannot be read back.

    :param patient: a ``maxillo.Patient``.
    :returns: ``{"upper": <FileRegistry>, "lower": <FileRegistry>}``, or ``None``.
    """
    from common.modality_config import (
        modality_prefers_processed_for_viewer,
        raw_file_hidden,
    )

    processed = patient.get_ios_processed_files()
    raw = patient.get_ios_raw_files()

    processed_pair = None
    if processed["upper"] and processed["lower"]:
        processed_pair = {"upper": processed["upper"], "lower": processed["lower"]}

    raw_pair = None
    if raw["upper"] and raw["lower"]:
        candidate = {"upper": raw["upper"], "lower": raw["lower"]}
        if not any(raw_file_hidden(file_obj) for file_obj in candidate.values()):
            raw_pair = candidate

    pairs = (
        (processed_pair, raw_pair)
        if modality_prefers_processed_for_viewer("ios")
        else (raw_pair, processed_pair)
    )
    return next((pair for pair in pairs if pair is not None), None)


def jaw_of(patient, file_obj):
    """Which arch a mesh row is, or ``None`` if it is not this patient's current pair.

    Used by the save path to refuse an anchor the viewer would not have served, so a
    client cannot file landmarks against a superseded scan by quoting its id.
    """
    pair = current_ios_pair(patient)
    if not pair:
        return None
    for jaw in JAWS:
        if pair[jaw].pk == getattr(file_obj, "pk", None):
            return jaw
    return None
