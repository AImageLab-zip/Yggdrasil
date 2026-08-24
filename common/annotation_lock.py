"""Raw data becomes immutable once annotation work exists for a patient.

Annotations are anchored to the exact bytes they were drawn on: swap a patient's
raw scan and every landmark, polygon and marker silently starts describing a
different volume, with nothing in the record to say so. So once *any* annotation
work exists for a patient, that patient's raw inputs are frozen for good.

"Raw" means a ``FileRegistry`` row whose ``file_type`` ends in ``_raw`` -- every
domain keeps its bytes there and ``file_path`` is an object-storage key -- plus
the legacy raw ``FileField``s still on ``laparoscopy.Patient``. Processed and
derived rows stay editable; the lock is about inputs.

There are two locks, because the panoramic is both a trigger and a target. A
user-edited panoramic arch is annotation work and freezes the raw scan, but it
must not freeze *itself*, or the first edit would be the last one allowed. Guard
raw files with :func:`raw_data_is_locked` and the panoramic editor with
:func:`panoramic_is_locked`.

Enforced in:
  - ``maxillo.admin.FileRegistryAdmin`` and ``laparoscopy.admin.PatientAdmin``
    -- the only place with an override, for superusers doing data repair
  - ``maxillo.views.file_management.add_raw_file`` / ``delete_raw_file``
  - ``maxillo.views.patient_data.save_browser_panoramic``

Nothing in the app UI can bypass the lock. Re-running processing is deliberately
still allowed: it re-derives outputs from the same frozen raw bytes.
"""


def is_raw_file_type(file_type):
    """Whether a ``FileRegistry.file_type`` names a raw input.

    Matches the ``"_raw" in ...`` convention already used by
    ``maxillo.views.file_management``, but anchored to the end so a future
    ``*_raw_preview`` type is not mistaken for the input itself.
    """
    return str(file_type or "").endswith("_raw")


def _lock_reasons(patient, include_panoramic):
    """Yield reasons lazily, so a boolean caller can stop at the first one.

    That matters: the admin changelists show a per-row lock column, and a
    generator turns "up to five queries per row" into "one" for a locked case.
    """
    if patient is None:
        return

    domain = patient._meta.app_label

    # Voice captions live on all three domains (common.base_models.VoiceCaptionBase).
    if patient.voice_captions.exists():
        yield "voice captions"

    if domain == "maxillo":
        if patient.classifications.exists():
            yield "an occlusion classification"
        if patient.intraoral_segmentations.exists():
            yield "tooth segmentation"
        if patient.files.filter(file_type="ios_landmarks").exists():
            yield "IOS landmarks"
        if include_panoramic:
            # Imported here: common must not import a domain app at module scope.
            from maxillo.models import PanoramicState

            if PanoramicState.objects.filter(
                patient=patient, geometry_source="custom_cp"
            ).exists():
                yield "an edited panoramic arch"
    elif domain == "laparoscopy":
        if patient.classifications.exists():
            yield "an occlusion classification"
        if patient.quadrant_markers.exists():
            yield "quadrant markers"
        if patient.region_annotations.exists():
            yield "region annotations"
    # brain has no annotation models of its own yet; voice captions are it.


def annotation_lock_reasons(patient, include_panoramic=True):
    """Human-readable reasons this patient's raw data is frozen; empty == open.

    Every trigger is reported, not just the first, so the caller can tell the
    user *what* locked the case. Pass ``include_panoramic=False`` to ask the
    question the panoramic editor needs: "is there annotation work here other
    than the panoramic itself?".

    Machine output is not annotation work: ``ios_landmarks_prediction`` and an
    ``auto`` panoramic geometry are both ignored.
    """
    return list(_lock_reasons(patient, include_panoramic))


def raw_data_is_locked(patient):
    """Whether this patient's raw inputs may no longer be added to or removed."""
    return any(_lock_reasons(patient, include_panoramic=True))


def panoramic_is_locked(patient):
    """Whether the panoramic arch may no longer be edited or regenerated.

    Excludes the patient's own panoramic state, so editing an arch does not lock
    the editor behind the user who just used it.
    """
    return any(_lock_reasons(patient, include_panoramic=False))


def lock_message(reasons, subject="raw files"):
    """One sentence explaining a refusal, for API errors and UI banners.

    Phrased so the verb never has to agree with ``subject`` -- it is a noun
    phrase supplied by the caller ("raw files", "panoramic arch").
    """
    if not reasons:
        return ""
    if len(reasons) == 1:
        listed = reasons[0]
    else:
        listed = f"{', '.join(reasons[:-1])} and {reasons[-1]}"
    listed = listed[:1].upper() + listed[1:]
    return (
        f"{listed} already exist for this patient, so the {subject} can no "
        "longer be changed."
    )
