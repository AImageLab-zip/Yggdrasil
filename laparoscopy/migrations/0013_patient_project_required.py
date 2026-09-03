"""Patients always belong to a project: repair orphans, then require it.

Companion to 0012_folder_project_required, which did the same for folders.
``Patient.project`` was added as nullable so 0010_folders_to_projects could
backfill it, and stayed nullable, which left it optional in every ModelForm --
the Django admin's "add patient" page included. A project-less patient is
invisible to the patient list, the export builder and every permission check that
resolves rights through the project.

Repair rule:

* a patient that already has a folder takes **that folder's** project, since the
  folder is what actually places it (and folders are project-scoped as of
  0012_folder_project_required); assigning the catch-all instead would leave the
  patient in a project its own folder does not belong to;
* a patient with no folder lands in the domain's catch-all project, in that
  project's "Uncategorized" folder (created if absent).

The repair is deliberately self-contained rather than shared across the three
domain apps: a migration must keep behaving the same forever, which it cannot do
if it calls into code that is still evolving.
"""

from django.db import migrations, models
import django.db.models.deletion

DOMAIN = "laparoscopy"
UNCATEGORIZED = "Uncategorized"


def _catchall_project(Project):
    """The project a homeless patient belongs to.

    Preference order: the domain's catch-all project (slug == domain, created by
    the folder->project migration), then its oldest project, then a freshly
    created catch-all. The last case only arises on a database that has patients
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


def _uncategorized_folder(Folder, project):
    """The catch-all folder of a project, created on first need."""
    folder = Folder.objects.filter(
        project=project, parent__isnull=True, name=UNCATEGORIZED
    ).first()
    if folder is not None:
        return folder
    return Folder.objects.create(name=UNCATEGORIZED, parent=None, project=project)


def forwards(apps, schema_editor):
    Patient = apps.get_model("laparoscopy", "Patient")
    Folder = apps.get_model("laparoscopy", "Folder")
    Project = apps.get_model("common", "Project")

    # _base_manager, not objects: the patient default manager hides soft-deleted
    # rows, and a soft-deleted orphan would still block the NOT NULL constraint.
    orphans = list(
        Patient._base_manager.filter(project__isnull=True)
        .select_related("folder")
        .order_by("pk")
    )
    if not orphans:
        return

    catchall = None
    fallback_folder = None
    for patient in orphans:
        folder = patient.folder
        if folder is not None and folder.project_id is not None:
            patient.project_id = folder.project_id
            patient.save(update_fields=["project"])
            continue

        if catchall is None:
            catchall = _catchall_project(Project)
            fallback_folder = _uncategorized_folder(Folder, catchall)
        patient.project = catchall
        patient.folder = fallback_folder
        patient.save(update_fields=["project", "folder"])


class Migration(migrations.Migration):

    dependencies = [
        ("laparoscopy", "0012_folder_project_required"),
        # Declared explicitly because forwards() resolves common.Project.
        ("common", "0045_cleanup_annotation_methods"),
    ]

    operations = [
        # Repair first: the column cannot be made NOT NULL while orphans exist.
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="patient",
            name="project",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="laparoscopy_patients",
                to="common.project",
            ),
        ),
    ]
