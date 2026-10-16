"""Assisted segmentation: the DjangoWorker GPU worker seen from Yggdrasil.

The browser never talks to the worker. ``common.consumers.AssistSessionConsumer`` holds the
browser's WebSocket for the page visit; everything the worker is told goes through here.
"""

import base64
import binascii
import json
import math
import secrets
import urllib.request

from django.conf import settings

from common.object_storage import get_object_storage

METHOD_SLUG = "assisted_segmentation"
MAX_POINTS = 64
MAX_WINDOW = 50
# The worker downloads the video once, when its session opens; this only has to outlast
# that wait (seconds, or minutes behind other sessions' loads).
LINK_TTL_SECONDS = 600


class PromptError(ValueError):
    """A prompt from the browser that the worker must not see."""


def is_configured():
    return bool(settings.ASSIST_WORKER_URL and settings.ASSIST_WORKER_TOKEN)


def new_session_id(file_id):
    # New per relay connection: a page reload's late DELETE must not evict the new session.
    return f"lap-{file_id}-{secrets.token_hex(4)}"


def video_link(key):
    """A signed, short-lived download link for one stored video.

    The worker gets this, never storage credentials: it can read this one file, for
    ``LINK_TTL_SECONDS``, and nothing else in the bucket.
    """
    return get_object_storage().presign_get(key, expires_seconds=LINK_TTL_SECONDS)


def _base_url():
    url = settings.ASSIST_WORKER_URL.rstrip("/")
    # Only ever http(s): urllib would happily open file:// or a custom scheme.
    if not url.startswith(("http://", "https://")):
        raise ValueError("ASSIST_WORKER_URL must be an http(s) URL")
    return url


def upstream_ws_url(session_id):
    scheme, rest = _base_url().split("://", 1)
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}/ws/session/{session_id}/"


def auth_headers():
    return {"Authorization": f"Bearer {settings.ASSIST_WORKER_TOKEN}"}


def worker_request(method, path, body=None):
    """One call to the worker's HTTP API. Blocking: from async code, run it in a thread."""
    request = urllib.request.Request(  # noqa: S310 -- scheme checked in _base_url()
        _base_url() + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={**auth_headers(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=settings.ASSIST_CONNECT_TIMEOUT) as response:  # noqa: S310
        return response.status


def probe_of(file_obj):
    """Width, height, fps and frame count of a video with a recorded probe, else ``None``."""
    probe = (file_obj.metadata or {}).get("probe") or {}
    try:
        width, height = int(probe["width"]), int(probe["height"])
        fps, frame_count = float(probe["fps"]), int(probe["frame_count"])
    except (KeyError, TypeError, ValueError):
        return None
    if min(width, height, frame_count) <= 0 or not fps > 0:
        return None
    return {"width": width, "height": height, "fps": fps, "frame_count": frame_count}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def prompt_body(session_id, message, probe):
    """The worker's ``/api/session/prompt/`` body for one browser prompt.

    Checked against the video's own probe: a frame of this video, points inside the frame,
    a mask of exactly this frame's size. ``window`` is how many frames to track forward after
    the prompted one (0: that frame only).
    """
    frame_index = message.get("frameIndex")
    if type(frame_index) is not int or not 0 <= frame_index < probe["frame_count"]:
        raise PromptError("That is not a frame of this video.")
    window = message.get("window", 0)
    if type(window) is not int or not 0 <= window <= MAX_WINDOW:
        raise PromptError(f"Propagate over 0 to {MAX_WINDOW} frames.")
    points, labels = message.get("points"), message.get("labels")
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS:
        raise PromptError(f"Send between 1 and {MAX_POINTS} points.")
    if (
        not isinstance(labels, list)
        or len(labels) != len(points)
        or any(type(label) is not int or label not in (0, 1) for label in labels)
    ):
        raise PromptError("Each point needs an include (1) or exclude (0) label.")
    for point in points:
        if (
            not isinstance(point, list)
            or len(point) != 2
            or not all(_is_number(value) for value in point)
            or not (0 <= point[0] < probe["width"] and 0 <= point[1] < probe["height"])
        ):
            raise PromptError("A point lies outside the frame.")
    region = {
        "region_id": "1",
        "points": [[float(x), float(y)] for x, y in points],
        "point_labels": labels,
    }
    mask = message.get("mask")
    if mask is not None:
        try:
            raw = base64.b64decode(mask, validate=True)
        except (TypeError, ValueError, binascii.Error):
            raise PromptError("The mask is not base64.")
        if len(raw) != math.ceil(probe["width"] * probe["height"] / 8):
            raise PromptError("The mask is not this frame's size.")
        region.update(
            mask_b64=mask,
            mask_encoding="bitpack_u1_v1",
            mask_shape=[probe["height"], probe["width"]],
        )
    return {
        "video_id": session_id,
        "frame_index": frame_index,
        # The worker tracks int(window_seconds * fps) frames; the half frame keeps float
        # error from rounding that down to window - 1.
        "window_seconds": (window + 0.5) / probe["fps"] if window else 0,
        "normalized": False,
        "regions": [region],
    }
