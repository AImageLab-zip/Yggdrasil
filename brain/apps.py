from django.apps import AppConfig


class BrainConfig(AppConfig):
	default_auto_field = "django.db.models.BigAutoField"
	name = "brain"

	def ready(self):
		from .export_config import install_brain_export_mappings

		install_brain_export_mappings()
