from django.urls import path
from . import api_views
from annotations import views as annotations_views

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
    # Same view, filename-suffixed. Cornerstone's NIfTI loader does `new URL(url)`
    # (which throws on a relative path) and then tests `pathname.endsWith('.gz')`
    # (which excludes the query string), so `?ext=.gz` cannot help -- finding F3 of
    # docs/cornerstone-roadmap.md. The suffix must be the last path segment and must
    # carry no trailing slash, which is why `file_key` stays a query parameter.
    # `filename` is decorative: it never takes part in resolving the file.
    path(
        "processing/files/serve/<int:file_id>/<str:filename>",
        api_views.serve_file,
        name="api_serve_file_named",
    ),
    # Same view again, with the bundle key in the path. Finding F14: the NIfTI
    # loader appends `?frame=N` with a literal `?`, so a URL that already carries
    # `?file_key=` produces two of them and every slice resolves to frame 0. The
    # maxillo CBCT display volume *is* a bundle member, so the viewer needs a
    # query-free way to name one. `filename` stays decorative here too.
    path(
        "processing/files/serve/<int:file_id>/key/<str:bundle_key>/<str:filename>",
        api_views.serve_file,
        name="api_serve_file_bundle",
    ),
    # Measurements made in the volume grid become durable annotation revisions.
    # Domain-oriented on purpose (the governing architectural rule): the URL names a
    # patient and the work, not a viewer. See annotations/views.py.
    path(
        "patients/<int:patient_id>/measurements/",
        annotations_views.save_measurements_api,
        name="api_save_measurements",
    ),
    path(
        "patients/<int:patient_id>/measurements/state/",
        annotations_views.measurements_state_api,
        name="api_measurements_state",
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
