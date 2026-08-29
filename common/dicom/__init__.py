"""Native DICOM: the catalog, de-identification, ingest and WADO-RS serving.

Lives in ``common`` rather than ``annotations`` on purpose. ``annotations`` imports
``common`` (a ``SourceResource`` points at a ``FileRegistry`` row), so a DICOM catalog
inside ``annotations`` that the upload path had to reach would close that cycle.

What this package is *not*: a PACS. It stores what was uploaded, de-identified, and
serves back the minimum a viewer needs to render it. Query/retrieve, C-FIND, STOW and
the SEG/RTSTRUCT/SR adapters are not here and are not planned here.
"""
