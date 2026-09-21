"""ECG upload: validate the JSON shape, write bytes to storage, register the file.

No ``Job``/``ProcessingStep`` row is created here -- the ECG plot is rendered
entirely client-side (see ``static/js/cardiology/ecg_plot.js``), so there is no
processing pipeline for a cardiology upload to enter. See
``laparoscopy/file_utils.py::save_video_to_dataset`` for the shape this mirrors.
"""

import json

from common.models import FileRegistry, Modality
from common.uploads import entity_fk_kwargs, raw_key_prefix_for, upload_uploaded_file_to_storage


class InvalidEcgFile(ValueError):
    """The uploaded file is not a recognizable ECG JSON document."""


def validate_ecg_json(uploaded_file):
    """Structural check only -- no signal processing happens server-side.

    Raises :class:`InvalidEcgFile` with a user-facing message on failure.
    """
    try:
        uploaded_file.seek(0)
        payload = json.load(uploaded_file)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidEcgFile("The uploaded file is not valid JSON.") from exc
    finally:
        uploaded_file.seek(0)

    if not isinstance(payload, dict):
        raise InvalidEcgFile("The ECG JSON must be an object.")
    missing = [key for key in ("frequency", "dataX", "data") if key not in payload]
    if missing:
        raise InvalidEcgFile(f"The ECG JSON is missing required field(s): {', '.join(missing)}.")
    if not isinstance(payload["data"], list) or not payload["data"]:
        raise InvalidEcgFile("The ECG JSON 'data' field must be a non-empty list of leads.")
    for lead in payload["data"]:
        if not isinstance(lead, dict) or "title" not in lead or "values" not in lead:
            raise InvalidEcgFile("Each entry in 'data' must have a 'title' and 'values'.")
    return payload


def save_ecg_to_dataset(patient, ecg_file):
    """Validate, upload and register one patient's ECG recording.

    Returns the created ``FileRegistry`` row. Raises :class:`InvalidEcgFile` if
    the upload is not a recognizable ECG JSON document -- callers should surface
    that as a form error before anything is written to storage.
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
