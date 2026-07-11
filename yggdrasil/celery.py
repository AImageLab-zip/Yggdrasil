import os

from celery import Celery


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "yggdrasil.settings")

app = Celery("yggdrasil")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
