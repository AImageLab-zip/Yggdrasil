from django.apps import AppConfig


class MaxilloConfig(AppConfig):
	default_auto_field = "django.db.models.BigAutoField"
	name = "maxillo"
	label = "maxillo"
	verbose_name = "Maxillo"

	def ready(self):
		import maxillo.signals  # noqa: F401  (registers signal receivers)

		from common import domain_uploads

		from .upload_api import MaxilloUploadHandler

		domain_uploads.register("maxillo", MaxilloUploadHandler())
