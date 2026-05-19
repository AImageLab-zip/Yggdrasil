from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('maxillo', '0018_folderaccess'),
    ]

    operations = [
        migrations.RenameIndex(
            model_name='folderaccess',
            new_name='maxillo_fol_folder__5d4ea1_idx',
            old_name='maxillo_fol_folder__e922d8_idx',
        ),
        migrations.RenameIndex(
            model_name='folderaccess',
            new_name='maxillo_fol_user_id_76f702_idx',
            old_name='maxillo_fol_user_id_3f7f89_idx',
        ),
        migrations.RenameIndex(
            model_name='folderaccess',
            new_name='maxillo_fol_role_892c13_idx',
            old_name='maxillo_fol_role_275cb5_idx',
        ),
        migrations.RenameIndex(
            model_name='folderaccess',
            new_name='maxillo_fol_folder__94abc0_idx',
            old_name='maxillo_fol_folder__68c0eb_idx',
        ),
        migrations.RenameIndex(
            model_name='folderaccess',
            new_name='maxillo_fol_user_id_9517e4_idx',
            old_name='maxillo_fol_user_id_1801c8_idx',
        ),
    ]
