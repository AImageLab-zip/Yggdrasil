from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('brain', '0009_folderaccess'),
    ]

    operations = [
        migrations.RenameIndex(
            model_name='folderaccess',
            new_name='brain_folde_folder__0ad931_idx',
            old_name='brain_folde_folder__7178b6_idx',
        ),
        migrations.RenameIndex(
            model_name='folderaccess',
            new_name='brain_folde_user_id_4dc96c_idx',
            old_name='brain_folde_user_id_84dd07_idx',
        ),
        migrations.RenameIndex(
            model_name='folderaccess',
            new_name='brain_folde_role_fd5105_idx',
            old_name='brain_folde_role_7a5169_idx',
        ),
        migrations.RenameIndex(
            model_name='folderaccess',
            new_name='brain_folde_folder__230dc2_idx',
            old_name='brain_folde_folder__b3167d_idx',
        ),
        migrations.RenameIndex(
            model_name='folderaccess',
            new_name='brain_folde_user_id_20a59e_idx',
            old_name='brain_folde_user_id_15ceef_idx',
        ),
    ]
