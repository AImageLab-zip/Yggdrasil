from django.urls import path
from . import api_views

app_name = "api"

urlpatterns = [
    # Health
    path("processing/health/", api_views.health_check, name="api_health_check"),
    # Jobs
    path(
        "processing/jobs/",
        api_views.ProcessingJobListView.as_view(),
        name="api_processing_jobs",
    ),
    path(
        "processing/jobs/<int:job_id>/status/",
        api_views.get_job_status,
        name="api_get_job_status",
    ),
    # Runner callbacks
    path(
        "runner/jobs/<int:job_id>/claim/",
        api_views.runner_claim_job,
        name="api_runner_claim_job",
    ),
    path(
        "runner/jobs/<int:job_id>/complete/",
        api_views.runner_complete_job,
        name="api_runner_complete_job",
    ),
    path(
        "runner/jobs/<int:job_id>/fail/",
        api_views.runner_fail_job,
        name="api_runner_fail_job",
    ),
    # Files
    path(
        "processing/files/", api_views.get_file_registry, name="api_get_file_registry"
    ),
    path(
        "processing/files/serve/<int:file_id>/",
        api_views.serve_file,
        name="api_serve_file",
    ),
    # Project-based API endpoints
    path(
        "<str:project_slug>/upload/",
        api_views.project_upload_api,
        name="api_project_upload",
    ),
    path(
        "<str:project_slug>/folders/",
        api_views.get_project_folders,
        name="api_project_folders",
    ),
    path(
        "<str:project_slug>/patients/",
        api_views.project_patients_handler,
        name="api_project_patients",
    ),
    path(
        "<str:project_slug>/patients/<int:patient_id>/files/",
        api_views.get_patient_files,
        name="api_get_patient_files",
    ),
]
