"""Cardiology's export collector for the ``ecg.classification`` artifact.

Registered with ``common.export_catalog`` from ``CardiologyConfig.ready()``: the
rhythm call lives in ``annotations/``, which ``common`` may not import.
"""

import json

from annotations.queries import ecg_rhythm_values
from annotations.services.ecg_rhythm import ecg_rhythm_state

COLLECTOR = "ecg_classification"

#: A serialized call is ~250 bytes; the preview multiplies, it does not serialize.
_ESTIMATED_DOCUMENT_BYTES = 300


def collect_ecg_classification(patient, artifact):
    """Yield the patient's rhythm call as one JSON document, or nothing."""
    state = ecg_rhythm_state(patient)
    if not state["value"]:
        return
    annotator = state["annotator"]
    content = json.dumps(
        {
            "patient_id": patient.patient_id,
            "manual": {
                "value": {"code": state["value"], "label": state["label"]},
                "annotator": annotator.username if annotator else None,
                "timestamp": state["timestamp"].isoformat() if state["timestamp"] else None,
                "revision": state["revision"],
            },
        },
        indent=2,
    )
    yield (
        {
            "type": "document",
            "patient": patient,
            "artifact": artifact,
            "content": content,
            "filename": artifact.filename or "ecg_classification.json",
        },
        len(content.encode("utf-8")),
    )


def count_ecg_classifications(patients):
    count = len(ecg_rhythm_values(patients))
    return count, count * _ESTIMATED_DOCUMENT_BYTES
