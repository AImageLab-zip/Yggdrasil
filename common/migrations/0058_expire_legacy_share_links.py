"""Give every never-expiring public export link an expiry.

Links shared before 2.0 have ``expires_at = NULL`` and never expire. From this
release a link that anyone can open always expires, so each legacy public
share gets the default lifetime from the moment this migration runs --
recipients keep access for that long rather than losing it at deploy.
Login-required links may still be non-expiring and are left alone.
"""

from datetime import timedelta

from django.db import migrations
from django.utils import timezone

EXPORT_MODELS = (
    ("maxillo", "Export"),
    ("brain", "Export"),
    ("laparoscopy", "Export"),
    ("urology", "Export"),
)
LEGACY_LINK_DAYS = 30  # common.export_share.SHARE_EXPIRY_DEFAULT_DAYS at the time of writing


def expire_legacy_links(apps, schema_editor):
    expires_at = timezone.now() + timedelta(days=LEGACY_LINK_DAYS)
    for app_label, model_name in EXPORT_MODELS:
        Export = apps.get_model(app_label, model_name)
        Export.objects.filter(share_mode="public", expires_at__isnull=True).update(
            expires_at=expires_at
        )


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0057_merge_20260920_1624"),
        ("maxillo", "0033_remove_folder_is_demo"),
        ("brain", "0023_remove_folder_is_demo"),
        ("laparoscopy", "0015_remove_folder_is_demo"),
        ("urology", "0003_remove_patient_dataset_remove_folder_is_demo_and_more"),
    ]

    operations = [
        migrations.RunPython(expire_legacy_links, migrations.RunPython.noop),
    ]
