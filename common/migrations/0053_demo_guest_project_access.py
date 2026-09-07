"""Revoke every demo-guest grant, so the demo must be published deliberately.

Migration 0036 gave the guest a ``ProjectAccess`` on *every* active project
(``role="standard"``, which granted nothing: it is not in
``ProjectAccess.ROLE_CHOICES`` nor in ``common.permissions.READ_ROLES``). Guest
visibility came from the ``Folder.is_demo`` flag instead, which bypassed
``ProjectAccess`` entirely -- so those rows were inert and harmless. Migration
0043 then swept ``standard`` -> ``viewer`` along with every other legacy role,
leaving the guest holding a *readable* role on all of them, still inert.

The ``is_demo`` flag is now gone and a guest grant is the whole of the demo's
scope: any project the guest can read is readable by anyone on the internet,
since ``/demo/`` signs visitors in as that account without a password. Those
legacy rows would therefore turn, on this deploy, into a blanket publication of
every project on the instance.

They cannot be told apart from a deliberate grant any more (0043 made them
identical), so this fails closed and removes all of them. Re-granting the guest
``viewer`` on the projects that really are public is a deliberate admin action;
the project admin now shows, per project, whether the guest holds it.
"""

from django.conf import settings
from django.db import migrations


def revoke_guest_access(apps, schema_editor):
    ProjectAccess = apps.get_model("common", "ProjectAccess")

    username = getattr(settings, "DEMO_GUEST_USERNAME", "guest")
    ProjectAccess.objects.filter(user__username=username).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0052_delete_dicom_catalog"),
    ]

    operations = [
        # Irreversible by design: restoring a blanket public grant on rollback
        # would be the unsafe direction.
        migrations.RunPython(revoke_guest_access, migrations.RunPython.noop),
    ]
