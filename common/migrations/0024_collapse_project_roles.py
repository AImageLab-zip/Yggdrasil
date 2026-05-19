from django.db import migrations, models


def collapse_project_roles(apps, schema_editor):
    ProjectAccess = apps.get_model('common', 'ProjectAccess')
    Invitation = apps.get_model('common', 'Invitation')

    ProjectAccess.objects.exclude(role='admin').update(role='standard')
    Invitation.objects.exclude(role='admin').update(role='standard')


class Migration(migrations.Migration):
    dependencies = [
        ('common', '0023_rename_common_file_domain_22309d_idx_maxillo_fil_domain_760eb4_idx_and_more'),
    ]

    operations = [
        migrations.RunPython(collapse_project_roles, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='projectaccess',
            name='role',
            field=models.CharField(
                choices=[('standard', 'Standard User'), ('admin', 'Administrator')],
                default='standard',
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name='invitation',
            name='role',
            field=models.CharField(
                choices=[('standard', 'Standard User'), ('admin', 'Administrator')],
                default='standard',
                max_length=20,
            ),
        ),
    ]
