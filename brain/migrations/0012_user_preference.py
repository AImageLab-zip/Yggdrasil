from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('brain', '0011_merge_20260519_1335'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='UserPreference',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('report_language', models.CharField(choices=[('it', 'Italian'), ('en', 'English')], default='it', max_length=5)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='brain_preference',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'db_table': 'brain_user_preference',
            },
        ),
    ]
