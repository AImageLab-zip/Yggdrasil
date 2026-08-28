"""Declarative catalog of exportable artifacts and export filters.

Before this module the same modality -> file_type mapping existed three times
(``maxillo.views.export.EXPORT_MODALITY_FILE_TYPES``,
``ExportProcessor.MODALITY_TO_FILE_TYPES`` and
``brain.export_config.BRAIN_EXPORT_MODALITY_FILE_TYPES``) and had already drifted
apart, while the export form offered a fixed four-checkbox "content type" that
could not name the things people actually want (the CBCT segmentation on its own,
the panoramic MIP but not the X-ray, IOS landmarks, ...).

An **artifact** is one exportable thing: a group of ``FileRegistry`` rows, one
named output nested inside a row's ``metadata['files']``, or a database-derived
document with no file behind it at all (voice captions, occlusion
classification, tooth segmentation). Every artifact declares the modality that
owns it, so the export form can offer exactly the artifacts a project's own
modalities can produce, and only those that actually exist in the selected
folders.

A **filter** narrows the patient set. Filters are likewise derived from the
project: a project that does not collect IOS landmarks is never offered a
"has landmarks" filter.
"""

from django.db.models import Q

# Artifact content buckets. `raw` and `processed` double as the ZIP sub-folder
# name (preserved from the previous exporter so archive layouts stay stable);
# `derived` artifacts declare their own path.
BUCKET_RAW = "raw"
BUCKET_PROCESSED = "processed"
BUCKET_DERIVED = "derived"

BUCKET_LABELS = {
    BUCKET_RAW: "Raw",
    BUCKET_PROCESSED: "Processed",
    BUCKET_DERIVED: "Derived",
}


class Artifact:
    """One exportable thing.

    file-backed artifacts:
        ``file_types``  FileRegistry.file_type values that carry this artifact.
        ``subtypes``    optional allow-list on FileRegistry.subtype.
        ``exclude_subtypes`` optional deny-list (for "everything else" buckets).
        ``nested_key``  a key inside ``metadata['files']`` when the row is a
                        bundle (CBCT completions publish volume_nifti /
                        segmentation_nifti / inference_stats_json this way).
        ``generated_from`` optional ``metadata['generated_from']`` allow-list.

    database-backed artifacts:
        ``collector``   name of the collector in ExportProcessor that produces
                        the document (no FileRegistry row exists).
    """

    def __init__(
        self,
        key,
        modality,
        label,
        bucket,
        *,
        file_types=(),
        subtypes=None,
        exclude_subtypes=None,
        nested_key=None,
        generated_from=None,
        collector=None,
        filename=None,
        zip_dir=None,
        description="",
    ):
        self.key = key
        self.modality = modality
        self.label = label
        self.bucket = bucket
        self.file_types = tuple(file_types)
        self.subtypes = frozenset(subtypes) if subtypes else None
        self.exclude_subtypes = frozenset(exclude_subtypes) if exclude_subtypes else None
        self.nested_key = nested_key
        self.generated_from = frozenset(generated_from) if generated_from else None
        self.collector = collector
        self.filename = filename
        self.zip_dir = zip_dir
        self.description = description

    def __repr__(self):
        return f"<Artifact {self.key}>"

    @property
    def is_file_backed(self):
        return bool(self.file_types)

    def registry_q(self):
        """Q object selecting the FileRegistry rows this artifact can live in.

        Coarse on purpose: ``matches`` does the per-row work that SQL cannot
        (nested bundle keys, metadata provenance).
        """
        query = Q(file_type__in=self.file_types)
        if self.subtypes:
            query &= Q(subtype__in=sorted(self.subtypes))
        return query

    def matches(self, file_registry):
        """Whether one FileRegistry row belongs to this artifact."""
        if file_registry.file_type not in self.file_types:
            return False
        subtype = (file_registry.subtype or "").strip()
        if self.subtypes is not None and subtype not in self.subtypes:
            return False
        if self.exclude_subtypes is not None and subtype in self.exclude_subtypes:
            return False
        if self.generated_from is not None:
            metadata = file_registry.metadata if isinstance(file_registry.metadata, dict) else {}
            if metadata.get("generated_from") not in self.generated_from:
                return False
        if self.nested_key and self.resolve_output(file_registry) is None:
            # A named output the row does not carry: a segmentation artifact must
            # not claim a row that only holds the display volume.
            return False
        return True

    def resolve_output(self, file_registry):
        """Locate this artifact's payload in a row: ``{"path", "size"}`` or None.

        Pipeline completions have published their outputs three ways over time,
        and all three are still in the database:

        * one row per output, ``subtype`` naming it and ``file_path`` pointing at
          it (what the current runner writes);
        * one row whose ``metadata['files']`` bundles every output by name;
        * (for artifacts with no ``nested_key``) a plain single-file row.
        """
        if not self.nested_key:
            return {
                "path": file_registry.file_path,
                "size": int(file_registry.file_size or 0),
            }

        metadata = file_registry.metadata if isinstance(file_registry.metadata, dict) else {}
        bundled = (metadata.get("files") or {}).get(self.nested_key)
        if isinstance(bundled, dict) and bundled.get("path"):
            return {
                "path": bundled["path"],
                "size": int(bundled.get("size") or bundled.get("file_size") or 0),
            }

        if (file_registry.subtype or "").strip() == self.nested_key and file_registry.file_path:
            return {
                "path": file_registry.file_path,
                "size": int(file_registry.file_size or 0),
            }
        return None

    def zip_directory(self):
        """Sub-folder inside the patient folder, e.g. ``cbct/processed``."""
        if self.zip_dir:
            return self.zip_dir
        if self.modality:
            return f"{self.modality}/{self.bucket}"
        return self.bucket


def _mri_artifacts():
    """The five brain MRI channels: identical raw/processed shape each."""
    channels = [
        ("braintumor-mri-t1", "T1", "braintumor_mri_t1"),
        ("braintumor-mri-t1c", "T1c", "braintumor_mri_t1c"),
        ("braintumor-mri-t2", "T2", "braintumor_mri_t2"),
        ("braintumor-mri-flair", "FLAIR", "braintumor_mri_flair"),
        ("braintumor-mri-seg", "Segmentation", "braintumor_mri_seg"),
    ]
    artifacts = []
    for slug, label, prefix in channels:
        artifacts.append(Artifact(
            f"{slug}.raw", slug, f"{label} volume (raw)", BUCKET_RAW,
            file_types=[f"{prefix}_raw"],
        ))
        artifacts.append(Artifact(
            f"{slug}.processed", slug, f"{label} volume (processed)", BUCKET_PROCESSED,
            file_types=[f"{prefix}_processed"],
        ))
    return artifacts


# Cross-domain artifacts: available whenever the owning modality is enabled.
# `modality=None` marks a patient-level artifact that no single modality owns.
_SHARED_ARTIFACTS = [
    Artifact(
        "reports.captions", None, "Voice caption reports", BUCKET_DERIVED,
        collector="captions", zip_dir="reports",
        description="Text captions attached to the patient, one file per caption.",
    ),
]

_MAXILLO_ARTIFACTS = [
    # --- CBCT ---------------------------------------------------------------
    Artifact(
        "cbct.raw", "cbct", "Uploaded volume", BUCKET_RAW,
        file_types=["cbct_raw"],
        description="The .nii.gz as uploaded.",
    ),
    Artifact(
        "cbct.volume", "cbct", "Processed volume", BUCKET_PROCESSED,
        file_types=["cbct_processed"], nested_key="volume_nifti",
        filename="volume.nii.gz",
    ),
    Artifact(
        "cbct.segmentation", "cbct", "Segmentation", BUCKET_PROCESSED,
        file_types=["cbct_processed"], nested_key="segmentation_nifti",
        filename="segmentation.nii.gz",
    ),
    Artifact(
        "cbct.inference_stats", "cbct", "Inference statistics", BUCKET_PROCESSED,
        file_types=["cbct_processed"], nested_key="inference_stats_json",
        filename="inference_stats.json",
    ),
    Artifact(
        "cbct.panoramic_view", "cbct", "Panoramic image (pipeline)", BUCKET_DERIVED,
        file_types=["cbct_processed"], nested_key="panoramic_view",
        zip_dir="panoramic/generated", filename="panoramic_pipeline.png",
        description="Panoramic PNG published by the CBCT processing step, where one exists.",
    ),
    # --- Panoramic ----------------------------------------------------------
    Artifact(
        "panoramic.uploaded", "panoramic", "Uploaded panoramic", BUCKET_RAW,
        file_types=["panoramic_raw"],
    ),
    Artifact(
        "panoramic.mip", "panoramic", "Panoramic image (MIP)", BUCKET_DERIVED,
        file_types=["panoramic_processed"], subtypes=["mip"],
        zip_dir="panoramic/generated", filename="panoramic_mip.png",
        description="Reconstructed from the CBCT along the dental arch.",
    ),
    Artifact(
        "panoramic.raysum", "panoramic", "Panoramic image (X-ray)", BUCKET_DERIVED,
        file_types=["panoramic_processed"], subtypes=["raysum"],
        zip_dir="panoramic/generated", filename="panoramic_xray.png",
    ),
    Artifact(
        "panoramic.legacy", "panoramic", "Panoramic Z-sweep (legacy)", BUCKET_DERIVED,
        file_types=["panoramic_processed"], exclude_subtypes=["mip", "raysum"],
        zip_dir="panoramic/generated",
        description="Older server-generated sweep variants, where they still exist.",
    ),
    # --- IOS ----------------------------------------------------------------
    Artifact(
        "ios.raw", "ios", "Uploaded scans (upper + lower)", BUCKET_RAW,
        file_types=["ios_raw_upper", "ios_raw_lower"],
    ),
    Artifact(
        "ios.processed", "ios", "Oriented scans", BUCKET_PROCESSED,
        # Legacy rows carry the arch in the file_type; new completions use
        # ios_processed with subtype='upper'/'lower'. Both are exported.
        file_types=["ios_processed_upper", "ios_processed_lower", "ios_processed"],
    ),
    Artifact(
        "ios.landmarks", "ios", "Tooth landmarks", BUCKET_DERIVED,
        file_types=["ios_landmarks"], zip_dir="ios/landmarks",
    ),
    Artifact(
        "ios.landmarks_prediction", "ios", "Predicted tooth landmarks", BUCKET_DERIVED,
        file_types=["ios_landmarks_prediction"], zip_dir="ios/landmarks",
    ),
    Artifact(
        "ios.bite_classification", "ios", "Bite classification (pipeline output)", BUCKET_DERIVED,
        file_types=["bite_classification"], zip_dir="ios/bite_classification",
    ),
    # --- Intraoral photographs ---------------------------------------------
    Artifact(
        "intraoral-photo.raw", "intraoral-photo", "Uploaded photographs", BUCKET_RAW,
        file_types=["intraoral_raw"],
    ),
    Artifact(
        "intraoral-photo.processed", "intraoral-photo", "Processed photographs", BUCKET_PROCESSED,
        file_types=["intraoral_processed", "intraoral-photo_processed"],
    ),
    Artifact(
        "intraoral-photo.segmentation", "intraoral-photo", "Tooth segmentation polygons",
        BUCKET_DERIVED, collector="tooth_segmentation",
        zip_dir="intraoral-photo/segmentation",
    ),
    # --- Teleradiography ----------------------------------------------------
    Artifact(
        "teleradiography.raw", "teleradiography", "Uploaded image", BUCKET_RAW,
        file_types=["teleradiography_raw"],
    ),
    Artifact(
        "teleradiography.processed", "teleradiography", "Processed image", BUCKET_PROCESSED,
        file_types=["teleradiography_processed"],
    ),
    # --- Raw archive --------------------------------------------------------
    Artifact(
        "rawzip.raw", "rawzip", "Uploaded archive", BUCKET_RAW,
        file_types=["generic_raw"],
    ),
    Artifact(
        "rawzip.processed", "rawzip", "Processed archive", BUCKET_PROCESSED,
        file_types=["generic_processed"],
    ),
    # --- Patient-level ------------------------------------------------------
    Artifact(
        "classification.occlusion", None, "Occlusion classification", BUCKET_DERIVED,
        collector="occlusion", zip_dir="bite_classification",
        filename="classification.json",
        description="Manual and AI sagittal / vertical / transverse / midline classes.",
    ),
]

_LAPAROSCOPY_ARTIFACTS = [
    Artifact("video.raw", "video", "Uploaded video", BUCKET_RAW, file_types=["video_raw"]),
    Artifact(
        "video.processed", "video", "Subsampled video and masks", BUCKET_PROCESSED,
        file_types=["video_processed"],
    ),
]

ARTIFACTS_BY_DOMAIN = {
    "maxillo": _MAXILLO_ARTIFACTS + _SHARED_ARTIFACTS,
    "brain": _mri_artifacts() + _SHARED_ARTIFACTS,
    "laparoscopy": _LAPAROSCOPY_ARTIFACTS + _SHARED_ARTIFACTS,
}


def artifacts_for_domain(domain):
    return ARTIFACTS_BY_DOMAIN.get(domain, ARTIFACTS_BY_DOMAIN["maxillo"])


def artifact_by_key(domain, key):
    for artifact in artifacts_for_domain(domain):
        if artifact.key == key:
            return artifact
    return None


def artifacts_for_project(domain, modality_slugs):
    """Artifacts a project can produce: those owned by one of its modalities,
    plus the patient-level ones (``modality=None``)."""
    modality_slugs = set(modality_slugs or ())
    return [
        artifact
        for artifact in artifacts_for_domain(domain)
        if artifact.modality is None or artifact.modality in modality_slugs
    ]


def resolve_artifacts(domain, keys):
    """Artifact specs for the given keys, silently dropping unknown ones.

    Unknown keys are dropped rather than raising: an artifact can be renamed or
    retired while old Export rows still reference it.
    """
    known = {artifact.key: artifact for artifact in artifacts_for_domain(domain)}
    return [known[key] for key in keys or () if key in known]


# Legacy selections: an Export row written before artifacts existed carries
# modality_slugs + include_raw/include_processed/include_reports/
# include_bite_classification. Those rows must keep re-running unchanged.
def artifacts_from_legacy_selection(
    domain,
    modality_slugs,
    *,
    include_raw,
    include_processed,
    include_reports,
    include_bite_classification,
):
    modality_slugs = set(modality_slugs or ())
    wanted_buckets = set()
    if include_raw:
        wanted_buckets.add(BUCKET_RAW)
    if include_processed:
        wanted_buckets.add(BUCKET_PROCESSED)
        # "Processed" used to also mean every derived output of a selected
        # modality (panoramic PNGs, landmarks, pipeline bite JSON).
        wanted_buckets.add(BUCKET_DERIVED)

    selected = []
    for artifact in artifacts_for_domain(domain):
        if artifact.key == "reports.captions":
            if include_reports:
                selected.append(artifact)
            continue
        if artifact.key == "classification.occlusion":
            if include_bite_classification:
                selected.append(artifact)
            continue
        if artifact.modality not in modality_slugs:
            continue
        if artifact.bucket in wanted_buckets:
            selected.append(artifact)
    return selected


def file_types_for(artifacts):
    """Every FileRegistry.file_type reachable from these artifacts."""
    file_types = set()
    for artifact in artifacts:
        file_types.update(artifact.file_types)
    return file_types


def modality_slugs_for(artifacts):
    return {artifact.modality for artifact in artifacts if artifact.modality}


def collectors_for(artifacts):
    return {artifact.collector for artifact in artifacts if artifact.collector}


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
#
# Previously three near-identical `for key, value in filters.items()` loops
# (export_new's summary, export_preview's counting, ExportProcessor's real query)
# each re-implemented a fixed set of `has_cbct` / `has_ios` /
# `has_bite_classification` / `has_reports_<slug>` checks. They are now one
# declarative list, generated from the project so a project is never offered a
# filter for data it does not collect.

GROUP_MODALITY = "Modality presence"
GROUP_PROCESSING = "Processing status"
GROUP_ANNOTATION = "Annotation presence"
GROUP_PANORAMIC = "Panoramic"
GROUP_METADATA = "Patient metadata"

PROCESSING_STATES = [
    ("completed", "Processing completed"),
    ("failed", "Processing failed"),
    ("pending", "Processing pending or running"),
    ("none", "No processing job"),
]

# Annotation-presence filters, keyed by the AnnotationMethod slug the project
# must enable. `modality`, when set, must also be enabled.
_ANNOTATION_FILTERS = [
    {
        "suffix": "captions",
        "method": "voice_caption",
        "label": "Has voice captions",
    },
    {
        "suffix": "occlusion",
        "method": "classification",
        "label": "Has occlusion classification",
    },
    {
        "suffix": "bite_classification",
        "method": "bite_classification",
        "label": "Has bite classification",
    },
    {
        "suffix": "landmarks",
        "method": "ios_landmarks",
        "modality": "ios",
        "label": "Has IOS landmarks",
    },
    {
        "suffix": "tooth_segmentation",
        "method": "intraoral_segmentation",
        "modality": "intraoral-photo",
        "label": "Has tooth segmentation",
    },
]


def build_filters(domain, project, modality_slugs):
    """Filter descriptors this project can meaningfully offer.

    Each descriptor is a plain dict the template renders directly:
      ``id``      form field name (``filter_<id>``)
      ``label``   human label
      ``group``   section heading
      ``kind``    'bool' | 'choice' | 'text' | 'date'
      ``choices`` [(value, label)] for 'choice'
    """
    modality_slugs = [slug for slug in (modality_slugs or ()) if slug != "rawzip"]
    filters = []

    for slug in modality_slugs:
        filters.append({
            "id": f"modality_{slug}",
            "label": f"Has {slug}",
            "group": GROUP_MODALITY,
            "kind": "bool",
        })

    for step_slug, step_name in _project_step_labels(project, modality_slugs):
        filters.append({
            "id": f"status_{step_slug}",
            "label": step_name,
            "group": GROUP_PROCESSING,
            "kind": "choice",
            "choices": PROCESSING_STATES,
        })

    enabled_methods = set()
    if project is not None:
        enabled_methods = set(
            project.annotation_methods.filter(is_active=True).values_list("slug", flat=True)
        )
    for spec in _ANNOTATION_FILTERS:
        if spec["method"] not in enabled_methods:
            continue
        required_modality = spec.get("modality")
        if required_modality and required_modality not in modality_slugs:
            continue
        filters.append({
            "id": f"annotation_{spec['suffix']}",
            "label": spec["label"],
            "group": GROUP_ANNOTATION,
            "kind": "bool",
        })

    if domain == "maxillo" and "cbct" in modality_slugs:
        filters.append({
            "id": "panoramic_state",
            "label": "Panoramic",
            "group": GROUP_PANORAMIC,
            "kind": "choice",
            "choices": [
                ("any", "Has a generated panoramic"),
                ("auto", "Default (auto arch) only"),
                ("edited", "Arch edited by hand"),
                ("none", "No generated panoramic"),
            ],
        })

    filters.extend([
        {"id": "tags", "label": "Tags (comma separated)", "group": GROUP_METADATA, "kind": "text"},
        {"id": "uploaded_after", "label": "Uploaded on or after", "group": GROUP_METADATA, "kind": "date"},
        {"id": "uploaded_before", "label": "Uploaded on or before", "group": GROUP_METADATA, "kind": "date"},
        {"id": "uploaded_by", "label": "Uploaded by (username)", "group": GROUP_METADATA, "kind": "text"},
    ])
    return filters


def _project_step_labels(project, modality_slugs):
    """(step_slug, display name) for the enabled steps of a project's modalities."""
    from common.models import ProcessingStep

    try:
        steps = list(
            ProcessingStep.objects.filter(
                is_enabled=True, modality__slug__in=list(modality_slugs)
            ).select_related("modality").order_by("modality__name", "slug")
        )
    except Exception:  # noqa: BLE001 - table may not be migrated yet
        return []
    if project is not None:
        disabled = set(project.disabled_steps.values_list("slug", flat=True))
        steps = [step for step in steps if step.slug not in disabled]
    return [(step.slug, step.name) for step in steps]


def filters_from_form(payload):
    """Filter values from a submitted form, i.e. only the ``filter_*`` fields.

    A form post also carries ``folder_ids``, ``artifacts`` and the CSRF token;
    requiring the prefix is what keeps those out of the stored filter set.
    """
    return normalize_filters(
        {
            str(key)[len("filter_"):]: value
            for key, value in (payload or {}).items()
            if str(key).startswith("filter_")
        }
    )


def normalize_filters(payload):
    """Read an already-keyed filter mapping into ``{id: value}``, dropping empties.

    Used for the JSON preview endpoint's ``filters`` object and for a stored
    ``query_params['filters']``. Legacy keys (``has_cbct``,
    ``has_reports_<slug>``, ...) are translated so Export rows written before
    this module still re-run. Use :func:`filters_from_form` for a raw form post.
    """
    values = {}
    for raw_key, raw_value in (payload or {}).items():
        key = str(raw_key)
        # Tolerated here too: a stored payload may still carry the form prefix.
        if key.startswith("filter_"):
            key = key[len("filter_"):]
        value = raw_value
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value is None or value == "" or value is False:
            continue
        values[key] = value

    normalized = {}
    for key, value in values.items():
        if key == "has_bite_classification":
            normalized["annotation_occlusion"] = True
        elif key.startswith("has_reports_"):
            normalized["annotation_captions"] = True
        elif key.startswith("has_"):
            normalized[f"modality_{key[len('has_'):]}"] = True
        else:
            normalized[key] = value
    return normalized


def apply_filters(patients, domain, filters, *, artifacts=()):
    """Apply normalized filters to a patient queryset (AND across filters).

    Shared by the preview endpoint and the real export run so a preview count can
    never disagree with what the ZIP contains.
    """
    filters = filters or {}
    file_types = file_types_for(artifacts)

    for key, value in filters.items():
        if key.startswith("modality_"):
            slug = key[len("modality_"):]
            patients = _filter_has_modality(patients, domain, slug, file_types)
        elif key.startswith("status_"):
            patients = _filter_by_step_status(patients, key[len("status_"):], value)
        elif key.startswith("annotation_"):
            patients = _filter_has_annotation(patients, domain, key[len("annotation_"):])
        elif key == "panoramic_state":
            patients = _filter_panoramic_state(patients, domain, value)
        elif key == "tags":
            names = [name.strip() for name in str(value).split(",") if name.strip()]
            if names:
                patients = patients.filter(tags__name__in=names)
        elif key == "uploaded_after":
            patients = patients.filter(uploaded_at__date__gte=value)
        elif key == "uploaded_before":
            patients = patients.filter(uploaded_at__date__lte=value)
        elif key == "uploaded_by":
            patients = patients.filter(uploaded_by__username=str(value).strip())
    return patients.distinct()


def _filter_has_modality(patients, domain, slug, selected_file_types):
    """Patients carrying at least one file of this modality.

    Matches on the modality FK (set by every current writer) or on the artifact
    file types for that modality, so legacy rows with a null FK still count.
    """
    modality_file_types = set()
    for artifact in artifacts_for_domain(domain):
        if artifact.modality == slug:
            modality_file_types.update(artifact.file_types)
    # When the export selected specific artifacts, respect that narrowing: a
    # "has CBCT" filter alongside a segmentation-only selection means "has a
    # segmentation", not "has any CBCT file".
    if selected_file_types:
        narrowed = modality_file_types & set(selected_file_types)
        if narrowed:
            modality_file_types = narrowed
    condition = Q(files__modality__slug=slug)
    if modality_file_types:
        condition |= Q(files__file_type__in=sorted(modality_file_types))
    return patients.filter(condition)


def _filter_by_step_status(patients, step_slug, state):
    """Patients whose newest job for a step is in the requested state."""
    jobs = Q(jobs__modality_slug=step_slug)
    if state == "none":
        return patients.exclude(jobs__modality_slug=step_slug)
    if state == "pending":
        return patients.filter(
            jobs & Q(jobs__status__in=["pending", "processing", "dependency", "retrying"])
        )
    if state in {"completed", "failed"}:
        return patients.filter(jobs & Q(jobs__status=state))
    return patients


def _filter_has_annotation(patients, domain, suffix):
    if suffix == "captions":
        return patients.filter(voice_captions__text_caption__isnull=False).exclude(
            voice_captions__text_caption=""
        )
    if domain != "maxillo":
        # The remaining annotations only exist on maxillo patients; asking for
        # them elsewhere can only mean "no patients".
        return patients.none()
    if suffix == "occlusion":
        return patients.filter(classifications__isnull=False)
    if suffix == "bite_classification":
        return patients.filter(files__file_type="bite_classification")
    if suffix == "landmarks":
        return patients.filter(files__file_type="ios_landmarks")
    if suffix == "tooth_segmentation":
        # From `annotations/`, not the legacy `intraoral_segmentations` reverse FK: the
        # editor and the segmentation job both write through
        # `annotations.services.segmentation` now, so that table stops moving the moment
        # anybody edits a study and this filter would answer for the corpus as it was.
        from annotations.queries import with_tooth_segmentation

        return with_tooth_segmentation(patients)
    return patients


def _filter_panoramic_state(patients, domain, value):
    if domain != "maxillo":
        return patients
    if value == "none":
        return patients.filter(panoramic_state__isnull=True)
    if value == "auto":
        return patients.filter(panoramic_state__geometry_source="auto")
    if value == "edited":
        return patients.filter(panoramic_state__geometry_source="custom_cp")
    return patients.filter(panoramic_state__isnull=False)


def describe_filters(domain, project, modality_slugs, filters):
    """Human labels for the active filters, for the export's query summary."""
    known = {spec["id"]: spec for spec in build_filters(domain, project, modality_slugs)}
    described = []
    for key, value in (filters or {}).items():
        spec = known.get(key)
        label = spec["label"] if spec else key
        if spec and spec["kind"] == "bool":
            described.append(label)
        else:
            described.append(f"{label}: {value}")
    return described
