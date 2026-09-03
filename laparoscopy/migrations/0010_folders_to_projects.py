"""Data migration: folders become Projects (laparoscopy).

Rule: each top-level folder becomes a Project; the folder row itself is kept
and repointed as that project's default folder; subfolders (any depth) are
flattened into new folder rows under the project. Patients are repointed to
project + folder. FolderAccess rows fold into ProjectAccess (highest role
wins). Domain catch-all project (slug 'laparoscopy') absorbs patients with no
folder.
"""

from django.utils.text import slugify
from django.db import migrations

DOMAIN = "laparoscopy"

ROLE_MAP = {"standard": "viewer", "annotator": "annotator", "project_manager": "admin"}
ROLE_RANK = {"viewer": 0, "annotator": 1, "admin": 2}


def _unique_slug(Project, base):
    slug = slugify(base) or DOMAIN
    candidate, i = slug, 2
    while Project.objects.filter(slug=candidate).exists():
        candidate = f"{slug}-{i}"
        i += 1
    return candidate


def _unique_project_name(Project, base):
    name, i = base, 2
    while Project.objects.filter(domain=DOMAIN, name=name).exists():
        name = f"{base} {i}"
        i += 1
    return name


def _unique_folder_name(Folder, project, base):
    name, i = base, 2
    while Folder.objects.filter(project=project, parent__isnull=True, name=name).exists():
        name = f"{base} ({i})"
        i += 1
    return name


def _grant(ProjectAccess, user_id, project, role):
    existing = ProjectAccess.objects.filter(user_id=user_id, project=project).first()
    new_rank = ROLE_RANK.get(role, 0)
    if existing is None:
        ProjectAccess.objects.create(user_id=user_id, project=project, role=role)
    elif ROLE_RANK.get(existing.role, 0) < new_rank:
        existing.role = role
        existing.save(update_fields=["role"])


def forwards(apps, schema_editor):
    Project = apps.get_model("common", "Project")
    ProjectAccess = apps.get_model("common", "ProjectAccess")
    Modality = apps.get_model("common", "Modality")
    AnnotationMethod = apps.get_model("common", "AnnotationMethod")
    Folder = apps.get_model("laparoscopy", "Folder")
    Patient = apps.get_model("laparoscopy", "Patient")
    FolderAccess = apps.get_model("laparoscopy", "FolderAccess")

    # 1. Domain catch-all project.
    catchall = Project.objects.filter(slug=DOMAIN).first()
    if catchall is None:
        catchall = Project.objects.create(
            name=DOMAIN.title(), slug=DOMAIN, domain=DOMAIN
        )
    if not catchall.domain:
        catchall.domain = DOMAIN
        catchall.save(update_fields=["domain"])

    # Applicable annotation methods for this domain.
    domain_methods = AnnotationMethod.objects.filter(domain=DOMAIN)
    common_methods = AnnotationMethod.objects.filter(domain="")

    # 2. Each root folder -> project (folder row becomes its default folder).
    for root in Folder.objects.filter(parent__isnull=True).order_by("id"):
        project = Project.objects.create(
            name=_unique_project_name(Project, root.name or "Project"),
            slug=_unique_slug(Project, root.name or DOMAIN),
            domain=DOMAIN,
            created_by=root.created_by,
        )
        project.modalities.set(catchall.modalities.all())
        project.annotation_methods.set(domain_methods | common_methods)

        root.project = project
        root.save(update_fields=["project"])

        # Flatten subfolders (any depth) into new rows under the project.
        old_to_new = {root.id: root}
        queue = list(Folder.objects.filter(parent=root).order_by("id"))
        while queue:
            old = queue.pop(0)
            new = Folder.objects.create(
                name=_unique_folder_name(Folder, project, old.name or "Folder"),
                parent=None,
                project=project,
                created_by=old.created_by,
                is_demo=old.is_demo,
            )
            old_to_new[old.id] = new
            queue.extend(Folder.objects.filter(parent=old).order_by("id"))

        # Repoint patients of this subtree into project + mapped folder.
        old_ids = list(old_to_new.keys())
        for patient in Patient.objects.filter(folder_id__in=old_ids):
            patient.project = project
            patient.folder = old_to_new[patient.folder_id]
            patient.save(update_fields=["project", "folder"])

        # Fold this subtree's FolderAccess into ProjectAccess BEFORE the old
        # subfolder rows are deleted (their FolderAccess rows cascade away).
        for access in FolderAccess.objects.filter(folder_id__in=old_ids):
            _grant(ProjectAccess, access.user_id, project, ROLE_MAP.get(access.role, "viewer"))

        # Old subfolder rows are now dead.
        Folder.objects.filter(id__in=old_ids).exclude(id=root.id).delete()

    # 3. Patients with no folder -> catch-all project + its default folder.
    default_folder = Folder.objects.filter(project=catchall, parent__isnull=True).first()
    if default_folder is None:
        default_folder = Folder.objects.create(
            name="General", parent=None, project=catchall
        )
    for patient in Patient.objects.filter(folder__isnull=True):
        patient.project = catchall
        patient.folder = default_folder
        patient.save(update_fields=["project", "folder"])

    # 4. Orphan folders / patients (defensive): assign catch-all.
    for folder in Folder.objects.filter(project__isnull=True):
        folder.project = catchall
        folder.save(update_fields=["project"])
    for patient in Patient.objects.filter(project__isnull=True):
        patient.project = catchall
        patient.folder = patient.folder or default_folder
        patient.save(update_fields=["project", "folder"])

    # 5. Fold any remaining FolderAccess rows (orphan folders) into ProjectAccess.
    for access in FolderAccess.objects.select_related("folder").all():
        _grant(ProjectAccess, access.user_id, access.folder.project, ROLE_MAP.get(access.role, "viewer"))


class Migration(migrations.Migration):

    dependencies = [
        ("laparoscopy", "0009_alter_folder_unique_together_folder_project_and_more"),
        ("common", "0043_backfill_project_domain_and_roles"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
