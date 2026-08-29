"""Reading a video's dimensions and frame count with ``ffprobe``.

Extracted from ``LaparoscopyExportProcessor`` in Phase 10 unchanged. The rasterisation
command needs the frame size the strokes were drawn against -- the legacy
``RegionAnnotation`` rows record a time and a point list and never the dimensions -- and
that is the same question the export has always asked. Two implementations would be two
chances to disagree about ``avg_frame_rate`` versus ``nb_frames``, which is exactly the
sort of disagreement that moves every mask by a frame.
"""

import json
import math
import subprocess


def probe_video(local_video_path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_packets",
        "-show_entries",
        "stream=width,height,avg_frame_rate,nb_frames,nb_read_packets,duration",
        "-of",
        "json",
        local_video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffprobe is required in the web container to export laparoscopy videos."
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed for {local_video_path}: {(result.stderr or result.stdout).strip()}"
        )

    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError(f"No video stream found in {local_video_path}")
    stream = streams[0]

    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Invalid video dimensions in {local_video_path}")

    fps_raw = str(stream.get("avg_frame_rate") or "")
    fps = 0.0
    if "/" in fps_raw:
        num_raw, den_raw = fps_raw.split("/", 1)
        try:
            num = float(num_raw)
            den = float(den_raw)
            if den:
                fps = num / den
        except (TypeError, ValueError, ZeroDivisionError):
            fps = 0.0
    elif fps_raw:
        try:
            fps = float(fps_raw)
        except (TypeError, ValueError):
            fps = 0.0
    if not math.isfinite(fps) or fps <= 0:
        fps = 1.0

    frame_count = 0
    for key in ("nb_frames", "nb_read_packets"):
        raw_value = stream.get(key)
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            frame_count = parsed
            break
    if frame_count <= 0:
        try:
            duration = float(stream.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration > 0:
            frame_count = max(1, int(math.ceil(duration * fps - 1e-9)))
    if frame_count <= 0:
        raise RuntimeError(f"Could not determine frame count for {local_video_path}")

    return {
        "width": width,
        "height": height,
        "fps": float(fps),
        "frame_count": int(frame_count),
    }


#: Where a probe result is cached on the ``FileRegistry`` row.
METADATA_KEY = "probe"


def recorded_probe(file_row):
    """The cached probe for a video row, or ``None``.

    Cached rather than re-run because ``ffprobe`` needs the whole file, and a
    patient-detail page render cannot afford to download a surgical recording to answer
    "how many frames per second". The values do not change: they are properties of bytes
    that the annotation lock already refuses to let anyone replace once annotated.
    """
    probe = (getattr(file_row, "metadata", None) or {}).get(METADATA_KEY)
    if not isinstance(probe, dict):
        return None
    if not all(probe.get(key) for key in ("width", "height", "fps", "frame_count")):
        return None
    return probe


def probe_and_record(file_row, local_video_path):
    """Probe a video already on disk and cache the answer on its registry row.

    Called from the upload path, which has the file locally anyway, and from
    ``annotations_rasterize_video_masks``, which has to download it regardless. Anything
    else reads :func:`recorded_probe` and does without if nobody has recorded one --
    guessing a frame rate would put every annotation on the wrong frame of a 25 fps
    recording, and it would look right.
    """
    probe = probe_video(local_video_path)
    metadata = dict(getattr(file_row, "metadata", None) or {})
    metadata[METADATA_KEY] = probe
    file_row.metadata = metadata
    file_row.save(update_fields=["metadata"])
    return probe
