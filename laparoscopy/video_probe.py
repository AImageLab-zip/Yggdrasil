"""Reading a video's dimensions and frame count with ``ffprobe``.

Extracted from ``LaparoscopyExportProcessor`` in Phase 10 unchanged. The rasterisation
command needs the frame size the strokes were drawn against -- the legacy
``RegionAnnotation`` rows record a time and a point list and never the dimensions -- and
that is the same question the export has always asked. Two implementations would be two
chances to disagree about ``avg_frame_rate`` versus ``nb_frames``, which is exactly the
sort of disagreement that moves every mask by a frame.
"""

import contextlib
import json
import logging
import math
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)


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


def probe_and_record_stored(file_row):
    """Record the probe for a video that is already in object storage.

    The caller is job completion: the cluster writes the compressed and subsampled
    derivatives straight into the bucket, so unlike an upload there is no moment at
    which the bytes are on local disk. They have to be fetched once, which is why this
    is not done lazily on a page render -- :func:`recorded_probe` exists precisely so a
    patient-detail view never downloads a surgical recording to ask how many frames it
    has.

    **The derivative's own numbers, not the source's.** The subsampled track is one
    frame per second of the original, so its frame rate and frame count are nothing like
    the raw video's, and it is the track the annotator mounts. Copying the raw probe onto
    it would put every mask on the wrong frame while looking entirely correct -- the same
    failure guessing 30 for 25 fps produces.

    Returns the probe, or ``None`` when it could not be taken: a job must not be failed
    because ffprobe was unhappy, and a derivative with no probe is simply one the
    annotator declines to open, which the surface already explains.
    """
    from common.object_storage import download_to_tempfile

    key = getattr(file_row, "file_path", None)
    if not key:
        return None
    suffix = os.path.splitext(key)[1] or ".mp4"
    try:
        with download_to_tempfile(key, suffix=suffix) as local_path:
            return probe_and_record(file_row, local_path)
    except Exception:
        logger.exception("Could not probe stored video %s", key)
        return None


def probe_and_record_upload(file_row, uploaded_file):
    """Record the probe for a video that has just been uploaded.

    This is the caller the module was written for and **the one it never had**: the
    docstring above claimed the upload path recorded a probe, and
    ``_video_annotate_payload`` repeated the claim, but the only caller in the tree
    was ``annotations_rasterize_video_masks`` -- a command that visits *only*
    patients carrying legacy stroke rows. So a video uploaded after Phase 10 got no
    probe from anywhere, the annotator declined to mount for the rest of that file's
    life, and the page said "No video uploaded for this patient."

    Django spools anything past ``FILE_UPLOAD_MAX_MEMORY_SIZE`` to disk, which a
    surgical recording always is, so the usual path is ``temporary_file_path()`` and
    costs nothing. A small in-memory upload is written out once rather than being
    refused, because the size of the file is not a reason to know less about it.

    Returns the probe, or ``None`` when it could not be taken -- **an upload must not
    fail because ffprobe did**. The file is stored and playable either way; what a
    missing probe costs is the annotator, and the page now says so.
    """
    local_path = None
    scratch = None
    with contextlib.suppress(Exception):
        local_path = uploaded_file.temporary_file_path()

    try:
        if not local_path:
            suffix = os.path.splitext(getattr(uploaded_file, "name", "") or "")[1]
            fd, scratch = tempfile.mkstemp(prefix="ygg_probe_", suffix=suffix)
            with os.fdopen(fd, "wb") as handle:
                # The upload has already been read once by the storage helper, so
                # rewind rather than assuming the cursor is where it started.
                with contextlib.suppress(Exception):
                    uploaded_file.seek(0)
                for chunk in uploaded_file.chunks():
                    handle.write(chunk)
            local_path = scratch

        return probe_and_record(file_row, local_path)
    except Exception:
        logger.exception(
            "Could not probe uploaded video for file %s; it is stored and playable, "
            "but the annotator will not mount until laparoscopy_probe_videos runs.",
            getattr(file_row, "id", None),
        )
        return None
    finally:
        if scratch:
            with contextlib.suppress(Exception):
                os.remove(scratch)
