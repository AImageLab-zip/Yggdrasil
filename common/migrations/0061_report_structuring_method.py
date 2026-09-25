"""Data migration: register the annotation method that gates report structuring.

``common.external_config.structuring_enabled_for`` asks whether the patient's project
has the ``report_structuring`` method ticked, and the release notes tell administrators
to tick it -- but nothing created the row to tick, so the answer was always no and both
the button and the endpoint refused.

Creating the row enables nothing on its own: 0043 is what attached the common methods to
the projects that existed then, and it has already run. Every project starts without this
one, and a project administrator ticking it is the record of a deliberate decision to let
dictated text reach the configured language model.
"""

from django.db import migrations

SLUG = "report_structuring"
NAME = "Report structuring"
# Blank: structuring is offered in every domain that has a report template, the same way
# voice captions are.
DOMAIN = ""
ICON = "fas fa-wand-magic-sparkles"


def forwards(apps, schema_editor):
    AnnotationMethod = apps.get_model("common", "AnnotationMethod")
    AnnotationMethod.objects.get_or_create(
        slug=SLUG,
        defaults={"name": NAME, "domain": DOMAIN, "icon": ICON, "is_active": True},
    )


def backwards(apps, schema_editor):
    """Drop the row, but never the projects' choice to have used it.

    Deleting the method cascades nothing: ``Project.annotation_methods`` is an M2M, so
    only the links go. Re-running forwards leaves those projects unticked, which is the
    safe direction for a feature that sends clinical text to a third party.
    """
    AnnotationMethod = apps.get_model("common", "AnnotationMethod")
    AnnotationMethod.objects.filter(slug=SLUG).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0060_prompttemplate_captionreport"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
