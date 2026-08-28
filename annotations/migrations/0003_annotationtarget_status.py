"""Per-target lifecycle status.

Additive: ``sqlmigrate`` prints one ``ADD COLUMN ... NULL``, so the v2.0.0-dump rule
holds and a rollback needs no data work.

It exists because confirmation is claimed per *image*, not per patient.
``AnnotationSet.status`` is keyed ``(domain, patient, kind)``, so a patient with six
intraoral photographs had one flag for all six -- and the legacy conversion collapsed
their six ``IntraoralToothSegmentation.is_confirmed`` values onto it, last row winning.
A target is exactly the granularity the claim is made at, so the column goes there.

No backfill accompanies this. The conversion commands have never been run against
production (see docs/cornerstone-roadmap.md, Verification), so there are no collapsed
rows to repair; ``annotations_convert_legacy`` is corrected instead. If a conversion has
been run somewhere that matters, reconcile with a command -- not by editing rows here,
where a failure halfway cannot resume.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('annotations', '0002_seed_fdi_schema'),
    ]

    operations = [
        migrations.AddField(
            model_name='annotationtarget',
            name='status',
            field=models.CharField(blank=True, choices=[('draft', 'Draft'), ('confirmed', 'Confirmed'), ('superseded', 'Superseded')], default=None, help_text="Where the work on *this* target is in its lifecycle, or NULL when the set-level status is the only answer. Confirmation is per target because that is the granularity it is actually claimed at: a clinician confirms the polygons on one photograph, not every photograph the patient owns, and the set's own status cannot say which of several images was meant.", max_length=20, null=True),
        ),
    ]
