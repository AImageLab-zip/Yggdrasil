from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('maxillo', '0016_intraoral_tooth_segmentation'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='intraoraltoothsegmentation',
            name='is_confirmed',
            field=models.BooleanField(default=False, help_text='True when this image segmentation is reviewed and locked.'),
        ),
        migrations.AddField(
            model_name='intraoraltoothsegmentation',
            name='confirmed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='intraoraltoothsegmentation',
            name='confirmed_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='confirmed_intraoral_segmentations', to=settings.AUTH_USER_MODEL),
        ),
    ]
