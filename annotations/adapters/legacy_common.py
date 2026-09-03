"""Translating the one annotation surface every domain shares: voice captions.

A caption is annotation work -- it is the reason the raw-data lock exists on
brain, which has no other annotation model -- but it is not geometry. It becomes
an event on the whole study, carrying the transcript and the audio's duration.

Pure: legacy values in, descriptor dicts out.
"""

from django.core.exceptions import ValidationError

from annotations.adapters import descriptors


def voice_caption(*, transcript="", duration=None, modality="", status=""):
    """Convert one ``VoiceCaption`` row into an event descriptor.

    The transcript goes in ``value`` rather than a label, because it is free
    text by nature and no vocabulary covers it -- which is exactly the case the
    ``value`` column exists for.

    A caption with no transcript yet is still converted. The recording is the
    annotation work; the transcription is a machine step that may not have run,
    and refusing to convert a pending one would lose the fact that somebody
    recorded it.
    """
    if duration is not None:
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise ValidationError(f"duration must be a number, got {duration!r}")
        if float(duration) < 0:
            raise ValidationError("duration must not be negative")

    # Always present, so a caption whose transcription has not run yet is still
    # an assertion the service will accept rather than an empty event.
    attributes = {"has_transcript": bool(transcript)}
    if duration is not None:
        # Seconds, as the legacy column stores them. Not converted to the
        # model's millisecond convention because this is not a *position* in a
        # timeline -- it is a length, and rounding it would be a lossy change to
        # a value the cross-check compares against the original.
        attributes["duration_seconds"] = float(duration)
    if modality:
        attributes["modality"] = modality
    if status:
        attributes["processing_status"] = status

    return [
        descriptors.event(
            event_type="voice_caption",
            value=transcript or "",
            attributes=attributes,
        )
    ]
