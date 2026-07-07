"""Public demo entry point (Phase 7). A single GET route that logs the visitor
in as the shared read-only guest and redirects into the real portal."""

from django.urls import path

from . import demo

app_name = "demo"

urlpatterns = [
    path("", demo.demo_index, name="index"),
]
