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

**The lock is monotonic** (decision #18). ``AnnotationSet.ever_annotated`` is set
the first time human work is recorded and never cleared, so deleting the work
does not thaw the scan it was drawn on. That removes an escape hatch on purpose:
the bytes were already interpreted, and a record that can be quietly reopened is
one nobody can explain later. The superuser override in the Django admin is what
is left, and it stamps ``metadata['lock_override']`` so a later fingerprint
mismatch is still explainable.

**Two sources, for one release.** The predicate asks ``annotations`` first and
then the legacy per-domain tables, and takes the union. Decision #6 keeps the
legacy tables readable for one release as a cross-check before they are dropped,
and the union is what makes that window safe: if the Phase-2 conversion misses a
surface, the patient stays locked instead of silently becoming editable. The
legacy half -- ``_legacy_reasons`` and with it the last import of a domain app
from ``common`` -- is deleted in the release that drops those tables, gated on a
clean ``annotations_crosscheck`` in production.

Enforced in:
  - ``maxillo.admin.FileRegistryAdmin`` and ``laparoscopy.admin.PatientAdmin``
    -- the only place with an override, for superusers doing data repair
  - ``maxillo.views.file_management.add_raw_file`` / ``delete_raw_file``
  - ``maxillo.views.metadata.update_nifti_metadata`` -- an affine rewrite
    re-bases every coordinate drawn on the volume (decision #17)
  - ``maxillo.views.patient_data.save_browser_panoramic``

Nothing in the app UI can bypass the lock. Re-running processing is deliberately
still allowed: it re-derives outputs from the same frozen raw bytes.
"""

#: ``AnnotationSet.kind`` -> the phrase shown to a user. A kind missing from this
#: map still locks the patient; it just reports itself by its slug, because
#: failing to lock is a data-integrity problem and failing to phrase it nicely
#: is not.
_KIND_REASONS = {
    "voice_caption": "voice captions",
    "occlusion_classification": "an occlusion classification",
    "intraoral_segmentation": "tooth segmentation",
    "ios_landmarks": "IOS landmarks",
    "panoramic_arch": "an edited panoramic arch",
    "video_regions": "region annotations",
    "video_quadrants": "quadrant markers",
    "volume_segmentation": "volume segmentation",
    "measurements": "measurements",
}

#: The one kind that must not lock the editor that produces it.
_PANORAMIC_KIND = "panoramic_arch"


def is_raw_file_type(file_type):
    """Whether a ``FileRegistry.file_type`` names a raw input.

    Matches the ``"_raw" in ...`` convention already used by
    ``maxillo.views.file_management``, but anchored to the end so a future
    ``*_raw_preview`` type is not mistaken for the input itself.
    """
    return str(file_type or "").endswith("_raw")


def _annotation_set_reasons(patient, include_panoramic):
    """Reasons drawn from ``annotations``: one query, on the monotonic flag.

    ``ever_annotated`` is what makes this a single indexed lookup instead of the
    five per-domain existence checks the legacy half needs. It also puts the
    machine-output rule in the data rather than in this module: a revision whose
    origin is a prediction never sets the flag, so an ``ios_landmarks_prediction``
    or an ``auto`` panoramic geometry does not lock a case, without this function
    having to know either of those names.
    """
    # Imported here rather than at module scope: ``common`` is imported by the
    # annotation models, so a top-level import would be a cycle.
    from annotations.models import AnnotationSet
    from common.domains import fk_fields_for

    patient_fk, _ = fk_fields_for(patient._meta.app_label)
    kinds = (
        AnnotationSet.objects.filter(ever_annotated=True, **{patient_fk: patient})
        .values_list("kind", flat=True)
        .distinct()
    )
    for kind in kinds:
        if kind == _PANORAMIC_KIND and not include_panoramic:
            continue
        yield _KIND_REASONS.get(kind, kind)


def _legacy_reasons(patient, include_panoramic):
    """Reasons drawn from the per-domain tables that predate ``annotations``.

    Kept for the one release in which the legacy tables stay readable as a
    cross-check (decision #6). Lazy, so a boolean caller can stop at the first
    one: the admin changelists show a per-row lock column, and a generator turns
    "up to five queries per row" into "one" for a locked case.
    """
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


def _lock_reasons(patient, include_panoramic):
    """Yield reasons lazily, from both sources, without repeating one.

    ``annotations`` is asked first so a converted patient answers in one query
    and the legacy checks are only reached for whatever the conversion has not
    covered. A patient present in both -- the normal state during the
    cross-check release -- reports each reason once.
    """
    if patient is None:
        return

    seen = set()
    for reason in _annotation_set_reasons(patient, include_panoramic):
        if reason not in seen:
            seen.add(reason)
            yield reason
    for reason in _legacy_reasons(patient, include_panoramic):
        if reason not in seen:
            seen.add(reason)
            yield reason


def annotation_lock_reasons(patient, include_panoramic=True):
    """Human-readable reasons this patient's raw data is frozen; empty == open.

    Every trigger is reported, not just the first, so the caller can tell the
    user *what* locked the case. Pass ``include_panoramic=False`` to ask the
    question the panoramic editor needs: "is there annotation work here other
    than the panoramic itself?".

    Machine output is not annotation work: a prediction never sets
    ``ever_annotated``, and the legacy half ignores ``ios_landmarks_prediction``
    and an ``auto`` panoramic geometry for the same reason.
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
