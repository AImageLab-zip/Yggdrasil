from django.apps import AppConfig


class LaparoscopyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'laparoscopy'
    verbose_name = 'Laparoscopy'

    def ready(self):
        from common import domain_uploads

        from .upload_api import LaparoscopyUploadHandler

        domain_uploads.register('laparoscopy', LaparoscopyUploadHandler())
