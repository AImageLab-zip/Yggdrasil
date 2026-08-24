"""Folders always belong to a project: repair orphans, then require it.

``Folder.project`` was added as nullable so the folder->project migration
(0018_alter_folder_unique_together_remove_patient_folders_and_more) could
backfill it, and it stayed nullable afterwards. That left the field optional in
every ModelForm -- including the Django admin's "add folder" page -- so a folder
could still be created with no project at all. Such a folder is invisible in every
project-scoped listing (patient list, upload, export) while remaining reachable
by id.

This migration closes that: any remaining orphan is adopted by the domain's
catch-all project and renamed "Uncategorized" (it is a catch-all, not the project
it was mis-named after), and the column becomes NOT NULL so the case cannot recur.

The repair is deliberately self-contained rather than shared across the three
domain apps: a migration must keep behaving the same forever, which it cannot do
if it calls into code that is still evolving.
"""

from django.db import migrations, models
import django.db.models.deletion

DOMAIN = "brain"
ORPHAN_FOLDER_NAME = "Uncategorized"


def _catchall_project(Project):
    """The project an orphan folder belongs to.

    Preference order: the domain's catch-all project (slug == domain, created by
    the folder->project migration), then its oldest project, then a freshly
    created catch-all. The last case only arises on a database that has folders
    but no project for this domain, and exists so the NOT NULL constraint below
    can always be satisfied.
    """
    project = Project.objects.filter(slug=DOMAIN, domain=DOMAIN).first()
    if project is not None:
        return project
    project = Project.objects.filter(domain=DOMAIN).order_by("id").first()
    if project is not None:
        return project
    return Project.objects.create(name=DOMAIN.title(), slug=DOMAIN, domain=DOMAIN)


def _unique_name(Folder, project, base):
    """`base`, suffixed if the project already has a root folder by that name.

    Folder rows are unique on (project, name, parent), so adopting an orphan must
    not collide with a folder the project already has.
    """
    name, index = base, 2
    while Folder.objects.filter(project=project, parent__isnull=True, name=name).exists():
        name = f"{base} ({index})"
        index += 1
    return name


def forwards(apps, schema_editor):
    Folder = apps.get_model("brain", "Folder")
    Project = apps.get_model("common", "Project")

    orphans = list(Folder.objects.filter(project__isnull=True).order_by("id"))
    if not orphans:
        return

    project = _catchall_project(Project)
    for folder in orphans:
        folder.project = project
        folder.name = _unique_name(Folder, project, ORPHAN_FOLDER_NAME)
        # A nested orphan is flattened to the project root, matching what the
        # folder->project migration did with sub-folders.
        folder.parent = None
        folder.save(update_fields=["project", "name", "parent"])


class Migration(migrations.Migration):

    dependencies = [
        ("brain", "0019_brainproject"),
        ("common", "0045_cleanup_annotation_methods"),
    ]

    operations = [
        # Repair first: the column cannot be made NOT NULL while orphans exist.
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="folder",
            name="project",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="brain_folders",
                to="common.project",
            ),
        ),
    ]
