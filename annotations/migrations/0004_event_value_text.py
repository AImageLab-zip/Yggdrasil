"""Widen ``EventAnnotationItem.value`` to hold what it is documented to hold.

``value`` is "free-text assertion, for statements no schema covers yet", and the
statement it actually carries on this platform is a **voice-caption transcript** --
which every domain stores in a ``TextField``. Declaring the destination
``CharField(max_length=255)`` meant the new schema could not represent the old one:
4072 of 4111 maxillo captions and all 6 brain captions are longer than 255 characters,
so ``annotations_convert_legacy`` died on the first real one with MySQL's
``Data too long for column 'value' at row 1`` and every caption went unconverted.

Widening only, and the column is in no index (`annotations_event_item` indexes
``revision``/``event_type``, ``target``/``time_ms`` and ``label``), so there is no
prefix-length question and no row can fail to fit.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("annotations", "0003_annotationtarget_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="eventannotationitem",
            name="value",
            field=models.TextField(
                blank=True,
                help_text="Free-text assertion, for statements no schema covers yet.",
            ),
        ),
    ]
