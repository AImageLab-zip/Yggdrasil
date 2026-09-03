"""Delete the native DICOM catalog.

The platform stores ``.nii.gz`` only from 3.0 onward. Production carries zero
``DicomSeries`` and zero ``DicomInstance`` rows, so this drops two empty tables
rather than migrating data.

``0046`` created them and cannot be edited -- ``0047``-``0051`` depend on it -- so the
deletion is expressed forward, here.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0051_modality_tab_labels"),
    ]

    operations = [
        # Instances first: DicomInstance carries the FK to DicomSeries.
        migrations.DeleteModel(name="DicomInstance"),
        migrations.DeleteModel(name="DicomSeries"),
    ]
