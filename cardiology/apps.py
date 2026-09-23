from django.apps import AppConfig


class CardiologyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cardiology'

    def ready(self):
        from common import export_catalog

        from .exports import COLLECTOR, collect_ecg_classification, count_ecg_classifications

        export_catalog.register_collector(
            COLLECTOR, produce=collect_ecg_classification, count=count_ecg_classifications
        )
