"""Anonymous public demo routes (Phase 7). GET/HEAD only, enforced per-view
by ``common.demo.demo_guard``."""

from django.urls import path

from . import demo

app_name = "demo"

urlpatterns = [
    path("", demo.demo_index, name="index"),
    path("<slug:domain>/", demo.demo_domain_list, name="domain"),
    path("<slug:domain>/patient/<int:pk>/", demo.demo_patient_detail, name="patient"),
    path("<slug:domain>/file/<int:file_id>/", demo.demo_serve_file, name="file"),
]
