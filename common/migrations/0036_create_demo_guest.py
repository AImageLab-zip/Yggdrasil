"""Create the shared read-only public-demo guest user.

Additive data migration (no schema change) — safe for the v1.9 restore path.
The guest gets a standard ProjectAccess on every active project and an unusable
password so it can never authenticate via the login form. It holds no
FolderAccess, so common.permissions grants it only is_demo folders.
"""

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import migrations


def create_demo_guest(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Project = apps.get_model("common", "Project")
    ProjectAccess = apps.get_model("common", "ProjectAccess")

    username = getattr(settings, "DEMO_GUEST_USERNAME", "guest")
    guest, _ = User.objects.get_or_create(username=username)
    # Historical models lack AbstractBaseUser helpers, so set the fields directly.
    guest.password = make_password(None)  # unusable password
    guest.is_active = True
    guest.is_staff = False
    guest.is_superuser = False
    guest.save()

    for project in Project.objects.filter(is_active=True):
        ProjectAccess.objects.get_or_create(
            user=guest, project=project, defaults={"role": "standard"}
        )


def remove_demo_guest(apps, schema_editor):
    User = apps.get_model("auth", "User")
    username = getattr(settings, "DEMO_GUEST_USERNAME", "guest")
    User.objects.filter(username=username).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0035_alter_processingstep_options_and_more"),
    ]

    operations = [
        migrations.RunPython(create_demo_guest, remove_demo_guest),
    ]
