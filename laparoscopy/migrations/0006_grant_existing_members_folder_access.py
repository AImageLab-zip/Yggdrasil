"""Grant standard FolderAccess on every laparoscopy folder to users who already
have laparoscopy ProjectAccess.

Before FolderAccess existed, every laparoscopy project member could see all
folders. This migration makes that implicit access explicit (and therefore
revocable) so the new folder-level ACL does not lock existing users out.
"""
from django.db import migrations


def grant_access(apps, schema_editor):
    Project = apps.get_model("common", "Project")
    ProjectAccess = apps.get_model("common", "ProjectAccess")
    Folder = apps.get_model("laparoscopy", "Folder")
    FolderAccess = apps.get_model("laparoscopy", "FolderAccess")

    project = Project.objects.filter(slug="laparoscopy").first()
    if project is None:
        return

    user_ids = list(
        ProjectAccess.objects.filter(project=project).values_list("user_id", flat=True)
    )
    folder_ids = list(Folder.objects.values_list("id", flat=True))

    rows = [
        FolderAccess(user_id=user_id, folder_id=folder_id, role="standard")
        for user_id in user_ids
        for folder_id in folder_ids
    ]
    FolderAccess.objects.bulk_create(rows, ignore_conflicts=True)


def revoke_access(apps, schema_editor):
    # Leave granted rows in place on reverse; the table itself is dropped by 0005.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("laparoscopy", "0005_folderaccess"),
        ("common", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(grant_access, revoke_access),
    ]
