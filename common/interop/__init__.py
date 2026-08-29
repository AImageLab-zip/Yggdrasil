"""Writing Yggdrasil's annotation record out as DICOM SEG, SR and RTSTRUCT.

Roadmap Phase 9. Export-side only, and server-authoritative: ``common/export_processing``
has no browser, so nothing here may depend on one. Import is deliberately not built --
see ``docs/cornerstone-future-work.md``.

**The constraint that shapes the whole package.** All three of these objects reference
*source DICOM instances*: a SEG carries ``ReferencedSeriesSequence`` and inherits the
source's Frame of Reference, an RT Structure Set names a referenced frame of reference
per ROI, and an SR's evidence is a list of composite objects. A patient whose CBCT
arrived as a ``.nii.gz`` has no DICOM identity to reference, and fabricating a Secondary
Capture series so an export had something to point at would file an invention as
provenance. So **interop artifacts are produced only for annotations anchored to a
stored DICOM series**, and a patient without one simply contributes no interop files.
:func:`common.interop.sources.series_for_resource` is the single place that asks.

Why ``common/interop`` and not ``common/dicom``: that package's own docstring says the
SEG/RTSTRUCT/SR adapters are not there and are not planned there, and it meant it --
``common/dicom`` is ingest and serving, which the *upload* path depends on. And not
``annotations/``, because the durable model must not know about interchange formats
(the governing architectural rule). This package is imported by the export and by
nothing else.

**RTSTRUCT is written against ``pydicom`` directly, not ``highdicom``.** The roadmap's
risk 13 says "highdicom's RTSTRUCT writer is newer and less exercised than its SEG
writer, which is why SEG and SR ship first". That premise is wrong: highdicom 0.28.1
has ``seg``, ``sr``, ``pm``, ``ann``, ``ko``, ``pr``, ``sc`` and ``legacy`` and **no
RTSTRUCT writer at all**, exercised or otherwise. The RT Structure Set IOD is small and
fully specified, so :mod:`common.interop.rtstruct` builds it attribute by attribute and
``common/tests_interop.py`` reads every written object back with ``pydicom`` and
re-derives the contours. That is a better proof than trusting a writer would have been.
"""

from common.interop.rtstruct import build_rtstruct
from common.interop.seg import build_seg
from common.interop.sources import (
    InteropUnavailable,
    derived_uid,
    instance_datasets,
    series_for_resource,
)
from common.interop.sr import build_sr

__all__ = [
    "InteropUnavailable",
    "build_rtstruct",
    "build_seg",
    "build_sr",
    "derived_uid",
    "instance_datasets",
    "series_for_resource",
]
