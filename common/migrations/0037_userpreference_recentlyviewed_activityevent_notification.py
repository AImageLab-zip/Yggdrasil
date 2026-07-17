"""2.0 frontend-revamp backend: cross-app prefs + recents + activity + notifications.

Purely additive (four new tables, no changes to existing schema) so the v1.9
mysqldump -> 2.0 migrate path stays intact.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("common", "0036_create_demo_guest"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserPreference",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("report_language", models.CharField(choices=[("it", "Italian"), ("en", "English"), ("de", "German")], default="it", max_length=5)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="ygg_preference", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="RecentlyViewed",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("domain", models.CharField(max_length=20)),
                ("patient_pk", models.IntegerField()),
                ("patient_name", models.CharField(blank=True, max_length=255)),
                ("project_label", models.CharField(blank=True, max_length=120)),
                ("icon", models.CharField(blank=True, max_length=40)),
                ("viewed_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="recently_viewed", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-viewed_at"],
            },
        ),
        migrations.CreateModel(
            name="ActivityEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("domain", models.CharField(max_length=20)),
                ("patient_pk", models.IntegerField(blank=True, null=True)),
                ("patient_name", models.CharField(blank=True, max_length=255)),
                ("verb", models.CharField(max_length=40)),
                ("target", models.CharField(blank=True, max_length=255)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="activity_events", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="Notification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("level", models.CharField(choices=[("info", "Info"), ("success", "Success"), ("warning", "Warning"), ("danger", "Danger")], default="info", max_length=10)),
                ("message", models.CharField(max_length=500)),
                ("url", models.CharField(blank=True, max_length=500)),
                ("is_read", models.BooleanField(db_index=True, default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="notifications", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="recentlyviewed",
            constraint=models.UniqueConstraint(fields=("user", "domain", "patient_pk"), name="common_recentlyviewed_uniq"),
        ),
        migrations.AddIndex(
            model_name="recentlyviewed",
            index=models.Index(fields=["user", "-viewed_at"], name="common_rv_user_viewed_idx"),
        ),
        migrations.AddIndex(
            model_name="activityevent",
            index=models.Index(fields=["domain", "patient_pk", "-created_at"], name="common_ae_dom_pat_idx"),
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(fields=["user", "is_read", "-created_at"], name="common_notif_user_read_idx"),
        ),
    ]
