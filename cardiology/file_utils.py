"""ECG upload: validate the JSON shape, write bytes to storage, register the file.

No ``Job``/``ProcessingStep`` row is created here -- the ECG plot is rendered
entirely client-side (see ``static/js/cardiology/ecg_plot.js``), so there is no
processing pipeline for a cardiology upload to enter. See
``laparoscopy/file_utils.py::save_video_to_dataset`` for the shape this mirrors.
"""

import json
import math

from common.models import FileRegistry, Modality
from common.uploads import entity_fk_kwargs, raw_key_prefix_for, upload_uploaded_file_to_storage

#: A 12-lead, 10 s, 500 Hz recording is well under 1 MB of JSON. The cap keeps
#: ``json.load`` -- which holds the whole document in memory in the web worker --
#: bounded, and a bulk upload of many files with it.
ECG_MAX_UPLOAD_BYTES = 20 * 1024 * 1024

#: The plot geometry of ``static/js/cardiology/ecg_plot.js`` (25 mm/s at 3 px/mm,
#: 30 mm rows), which draws the whole recording onto one canvas and uploads it as
#: the export PNG. A recording whose canvas a browser cannot allocate would never
#: plot and never save, so it is refused here, where the uploader can see why.
PLOT_PX_PER_SEC = 75
PLOT_ROW_PX = 90
PLOT_MARGIN_PX = 20
#: Chrome and Firefox refuse a canvas side past 32767 px; iOS Safari refuses an
#: area past ~16.7 Mpx. ``save_browser_ecg_plot`` enforces the same area.
PLOT_MAX_SIDE_PX = 32767
PLOT_MAX_PIXELS = 4000 * 4000


class InvalidEcgFile(ValueError):
    """The uploaded file is not a recognizable ECG JSON document."""


def _numbers(values, what):
    if not isinstance(values, list) or not values:
        raise InvalidEcgFile(f"{what} must be a non-empty list of numbers.")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise InvalidEcgFile(f"{what} must contain only finite numbers.")
    return values


def validate_ecg_json(uploaded_file):
    """Structural check only -- no signal processing happens server-side.

    Returns the parsed document; raises :class:`InvalidEcgFile` with a
    user-facing message on failure.
    """
    size = getattr(uploaded_file, "size", None)
    if size is not None and size > ECG_MAX_UPLOAD_BYTES:
        raise InvalidEcgFile(
            f"The ECG file is larger than {ECG_MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
    try:
        uploaded_file.seek(0)
        payload = json.load(uploaded_file)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise InvalidEcgFile("The uploaded file is not valid JSON.") from exc
    finally:
        uploaded_file.seek(0)

    if not isinstance(payload, dict):
        raise InvalidEcgFile("The ECG JSON must be an object.")
    missing = [key for key in ("frequency", "dataX", "data") if key not in payload]
    if missing:
        raise InvalidEcgFile(f"The ECG JSON is missing required field(s): {', '.join(missing)}.")

    frequency = payload["frequency"]
    if isinstance(frequency, bool) or not isinstance(frequency, (int, float)) or not (
        math.isfinite(frequency) and frequency > 0
    ):
        raise InvalidEcgFile("'frequency' must be a positive number.")

    time = _numbers(payload["dataX"], "'dataX'")
    if any(later < earlier for earlier, later in zip(time, time[1:])):
        raise InvalidEcgFile("'dataX' must be in increasing order (seconds).")

    leads = payload["data"]
    if not isinstance(leads, list) or not leads:
        raise InvalidEcgFile("The ECG JSON 'data' field must be a non-empty list of leads.")
    for lead in leads:
        if not isinstance(lead, dict) or "title" not in lead or "values" not in lead:
            raise InvalidEcgFile("Each entry in 'data' must have a 'title' and 'values'.")
        # Lengths may differ: the plotter truncates every lead and the time axis
        # to the shortest of them.
        _numbers(lead["values"], f"Lead {lead['title']!r} 'values'")

    width = math.ceil((time[-1] - time[0]) * PLOT_PX_PER_SEC) + PLOT_MARGIN_PX
    height = len(leads) * PLOT_ROW_PX
    if width > PLOT_MAX_SIDE_PX or width * height > PLOT_MAX_PIXELS:
        max_seconds = min(
            (PLOT_MAX_SIDE_PX - PLOT_MARGIN_PX) / PLOT_PX_PER_SEC,
            (PLOT_MAX_PIXELS / height - PLOT_MARGIN_PX) / PLOT_PX_PER_SEC,
        )
        raise InvalidEcgFile(
            f"The recording is too long to plot: {len(leads)} leads allow at most "
            f"{int(max_seconds)} s at the clinical 25 mm/s scale."
        )
    return payload


def save_ecg_to_dataset(patient, ecg_file):
    """Validate, upload and register one patient's ECG recording.

    Returns the created ``FileRegistry`` row. Raises :class:`InvalidEcgFile` if
    the upload is not a recognizable ECG JSON document -- nothing is written to
    storage in that case.
    """
    validate_ecg_json(ecg_file)

    key = f"{raw_key_prefix_for(patient, 'ecg')}/ecg_patient_{patient.patient_id}.json"
    key, file_size, file_hash = upload_uploaded_file_to_storage(key=key, uploaded_file=ecg_file)

    modality = Modality.objects.filter(slug='ecg').first()
    return FileRegistry.objects.create(
        file_type='ecg_raw',
        file_path=key,
        file_size=file_size,
        file_hash=file_hash,
        modality=modality,
        metadata={'original_filename': ecg_file.name},
        **entity_fk_kwargs(patient),
    )
