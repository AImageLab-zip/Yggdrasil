"""Presentation helpers that turn a project into an export form.

Kept out of the views because all three domains render the same
``maxillo/export_new.html``: maxillo (also serving laparoscopy) and brain each
have their own export view, and both need the identical folder tree, artifact
groups and filter groups built from ``common.export_catalog``.
"""

from django.db.models import Count

from common import export_catalog
from common.models import FileRegistry


def project_modalities(project):
    """Active modalities of a project, minus the raw-archive pseudo-modality."""
    if project is None:
        return []
    return [
        modality
        for modality in project.modalities.filter(is_active=True).order_by("name")
        if modality.slug != "rawzip"
    ]


def folder_tree(folders, patient_model, domain):
    """Folders as a flat, depth-annotated list, parents before children.

    Sub-folders used to be unreachable from the export builder (it queried
    ``parent__isnull=True``), so patients filed below the top level could not be
    exported at all.

    A folder whose parent is not in ``folders`` is treated as a root, so a folder
    the user may read is never hidden behind an ancestor they may not.
    """
    folders = list(folders)
    if not folders:
        return []

    counts = _patient_counts(folders, patient_model, domain)
    by_parent = {}
    for folder in folders:
        by_parent.setdefault(folder.parent_id, []).append(folder)
    visible_ids = {folder.id for folder in folders}

    ordered = []

    def walk(parent_id, depth):
        for folder in by_parent.get(parent_id, []):
            ordered.append({
                "folder": folder,
                "depth": depth,
                "patient_count": counts.get(folder.id, 0),
            })
            walk(folder.id, depth + 1)

    for folder in folders:
        if folder.parent_id not in visible_ids:
            ordered.append({
                "folder": folder,
                "depth": 0,
                "patient_count": counts.get(folder.id, 0),
            })
            walk(folder.id, 1)
    return ordered


def _patient_counts(folders, patient_model, domain):
    """folder id -> patient count.

    Every domain links a patient to one folder via the `folder` FK; brain's
    `folders` many-to-many was collapsed by the folder->project migration.
    """
    rows = (
        patient_model.objects.filter(folder__in=folders)
        .values("folder")
        .annotate(total=Count("patient_id", distinct=True))
    )
    return {row["folder"]: row["total"] for row in rows}


def artifact_groups(domain, project, patients, patient_fk="patient"):
    """Selectable artifacts grouped by modality, annotated with counts.

    Only the artifacts this project's modalities can produce, each with how many
    rows exist for the patients currently in scope. An artifact with none is
    marked unavailable so the form can grey it out instead of letting someone
    select something that would silently export nothing.
    """
    modalities = project_modalities(project)
    by_slug = {modality.slug: modality for modality in modalities}
    enabled_slugs = list(by_slug)
    artifacts = export_catalog.artifacts_for_project(domain, enabled_slugs)

    file_rows = None
    if patients is not None:
        file_rows = FileRegistry.objects.filter(
            domain=domain, **{f"{patient_fk}__in": patients}
        )

    groups = {}
    for artifact in artifacts:
        slug = artifact.modality or "patient"
        modality = by_slug.get(slug)
        group = groups.setdefault(slug, {
            "slug": slug,
            "name": modality.name if modality else "Patient level",
            "icon": modality.icon if modality and modality.icon else "fas fa-user",
            "buckets": {},
        })
        # No count for an artifact with a collector, even one that also has files:
        # its document is produced from the database, so a zero file count would
        # grey out a checkbox that has something to export.
        count = None
        if artifact.is_file_backed and not artifact.collector and file_rows is not None:
            count = file_rows.filter(artifact.registry_q()).count()
        group["buckets"].setdefault(artifact.bucket, []).append({
            "key": artifact.key,
            "label": artifact.label,
            "count": count,
            "available": count is None or count > 0,
        })

    ordered = []
    for slug in enabled_slugs + ["patient"]:
        group = groups.pop(slug, None)
        if group is None:
            continue
        group["buckets"] = [
            {
                "bucket": bucket,
                "label": export_catalog.BUCKET_LABELS[bucket],
                "artifacts": group["buckets"][bucket],
            }
            for bucket in (
                export_catalog.BUCKET_RAW,
                export_catalog.BUCKET_PROCESSED,
                export_catalog.BUCKET_DERIVED,
            )
            if bucket in group["buckets"]
        ]
        ordered.append(group)
    return ordered


def grouped_filters(domain, project, modality_slugs):
    """``build_filters`` output bucketed by display group, for the template."""
    grouped = []
    for spec in export_catalog.build_filters(domain, project, modality_slugs):
        if not grouped or grouped[-1]["group"] != spec["group"]:
            grouped.append({"group": spec["group"], "filters": []})
        grouped[-1]["filters"].append(spec)
    return grouped


def allowed_artifact_keys(domain, project):
    """Artifact keys a client may legitimately submit for this project."""
    if project is None:
        return set()
    slugs = list(project.modalities.values_list("slug", flat=True))
    return {
        artifact.key
        for artifact in export_catalog.artifacts_for_project(domain, slugs)
    }
