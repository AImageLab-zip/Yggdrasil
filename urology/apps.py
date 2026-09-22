from django.apps import AppConfig


class UrologyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "urology"
    verbose_name = "Urology"

    def ready(self):
        from .export_config import install_urology_export_mappings

        install_urology_export_mappings()
