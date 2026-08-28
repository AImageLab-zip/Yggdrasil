from django.shortcuts import redirect
from django.urls import path

from brain import api_views, views
from annotations import views as annotations_views


app_name = "brain"

urlpatterns = [
    path("", views.home, name="home"),
    path("patients/", views.patient_list, name="patient_list"),
    path("upload/", views.upload_patient, name="upload_patient"),
    path(
        "project/<int:project_id>/select/", views.select_project, name="select_project"
    ),
    path("patient/<int:patient_id>/", views.patient_detail, name="patient_detail"),
    path(
        "patient/<int:patient_id>/update-name/",
        views.update_patient_name,
        name="update_patient_name",
    ),
    path(
        "patient/<int:patient_id>/text-caption/",
        views.upload_text_caption,
        name="upload_text_caption",
    ),
    path(
        "patient/<int:patient_id>/voice-caption/<int:caption_id>/delete/",
        views.delete_voice_caption,
        name="delete_voice_caption",
    ),
    path(
        "patient/<int:patient_id>/voice-caption/<int:caption_id>/edit/",
        views.edit_voice_caption_transcription,
        name="edit_voice_caption_transcription",
    ),
    path(
        "patient/<int:patient_id>/voice-caption/<int:caption_id>/update-modality/",
        views.update_voice_caption_modality,
        name="update_voice_caption_modality",
    ),
    path(
        "patient/<int:patient_id>/tags/add/",
        views.add_patient_tag,
        name="add_patient_tag",
    ),
    path(
        "patient/<int:patient_id>/tags/remove/",
        views.remove_patient_tag,
        name="remove_patient_tag",
    ),
    path(
        "patient/<int:patient_id>/delete/", views.delete_patient, name="delete_patient"
    ),
    path(
        "patients/bulk-delete/", views.bulk_delete_patients, name="bulk_delete_patients"
    ),
    path(
        "patients/bulk-purge/", views.bulk_purge_patients, name="bulk_purge_patients"
    ),
    path(
        "patient/<int:patient_id>/rerun-processing/",
        views.rerun_processing,
        name="rerun_processing",
    ),
    path(
        "patients/bulk-rerun-processing/",
        views.bulk_rerun_processing,
        name="bulk_rerun_processing",
    ),
    path(
        "admin/control-panel/",
        lambda request: redirect("admin_control_panel"),
        name="admin_control_panel",
    ),
    path("profile/", views.user_profile, name="user_profile"),
    path(
        "profile/<str:username>/", views.user_profile, name="user_profile_by_username"
    ),
    path("folders/create/", views.create_folder, name="create_folder"),
    path("folders/<int:folder_id>/stats/", views.folder_stats, name="folder_stats"),
    path("folders/<int:folder_id>/rename/", views.rename_folder, name="rename_folder"),
    path("folders/<int:folder_id>/delete/", views.delete_folder, name="delete_folder"),
    path("folders/<int:folder_id>/permissions/", views.folder_permissions, name="folder_permissions"),
    path("folders/<int:folder_id>/permissions/upsert/", views.upsert_folder_permission, name="upsert_folder_permission"),
    path("folders/<int:folder_id>/permissions/<int:user_id>/delete/", views.delete_folder_permission, name="delete_folder_permission"),
    path(
        "folders/move-patients/",
        views.move_patients_to_folder,
        name="move_patients_to_folder",
    ),
    path(
        "folders/add-patients/",
        views.add_patients_to_folder,
        name="add_patients_to_folder",
    ),
    path(
        "folders/remove-patients/",
        views.remove_patients_from_folder,
        name="remove_patients_from_folder",
    ),
    path("export/", views.export_list, name="export_list"),
    path("export/new/", views.export_new, name="export_new"),
    path("export/preview/", views.export_preview, name="export_preview"),
    path("export/<int:export_id>/", views.export_status, name="export_status"),
    path(
        "export/<int:export_id>/download/",
        views.export_download,
        name="export_download",
    ),
    path(
        "export/<int:export_id>/share/",
        views.export_share_update,
        name="export_share_update",
    ),
    path(
        "export/shared/<str:share_token>/",
        views.export_shared_landing,
        name="export_shared_landing",
    ),
    path(
        "export/shared/<str:share_token>/download/",
        views.export_shared_download,
        name="export_shared_download",
    ),
    path("export/<int:export_id>/delete/", views.export_delete, name="export_delete"),
    path("export/<int:export_id>/stop/", views.export_stop, name="export_stop"),
    path(
        "api/patient/<int:patient_id>/data/",
        views.patient_viewer_data,
        name="patient_viewer_data",
    ),
    path(
        "api/patient/<int:patient_id>/cbct/",
        views.patient_cbct_data,
        name="patient_cbct_data",
    ),
    path(
        "api/patient/<int:patient_id>/panoramic/",
        views.patient_panoramic_data,
        name="patient_panoramic_data",
    ),
    path(
        "api/patient/<int:patient_id>/intraoral/",
        views.patient_intraoral_data,
        name="patient_intraoral_data",
    ),
    path(
        "api/patient/<int:patient_id>/intraoral-photo/",
        views.patient_intraoral_data,
        name="patient_intraoral_photo_data",
    ),
    # No `intraoral-segmentation/` routes: these were namespace parity with maxillo, and
    # maxillo's went with the Konva editor in Phase 5. Keeping brain's would leave two
    # routes answering for a surface brain does not have and whose maxillo counterpart no
    # longer exists.
    path(
        "api/patient/<int:patient_id>/teleradiography/",
        views.patient_teleradiography_data,
        name="patient_teleradiography_data",
    ),
    path(
        "api/patient/<int:patient_id>/volume/<slug:modality_slug>/",
        views.patient_volume_data,
        name="patient_volume_data",
    ),
    path(
        "api/patient/<int:patient_id>/nifti-metadata/",
        views.get_nifti_metadata,
        name="get_nifti_metadata",
    ),
    path(
        "api/patient/<int:patient_id>/nifti-metadata/update/",
        views.update_nifti_metadata,
        name="update_nifti_metadata",
    ),
    path("api/processing/health/", api_views.health_check, name="api_health_check"),
    path(
        "api/processing/jobs/",
        api_views.ProcessingJobListView.as_view(),
        name="api_processing_jobs",
    ),
    path(
        "api/processing/jobs/<int:job_id>/status/",
        api_views.get_job_status,
        name="api_get_job_status",
    ),
    # Runner callbacks intentionally removed — external runners use the single
    # token-authenticated contract at /api/runner/jobs/<id>/... (domain-agnostic).
    path(
        "api/processing/files/",
        api_views.get_file_registry,
        name="api_get_file_registry",
    ),
    path(
        "api/processing/files/serve/<int:file_id>/",
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
        "api/processing/files/serve/<int:file_id>/<str:filename>",
        api_views.serve_file,
        name="api_serve_file_named",
    ),
    # Same view again, with the bundle key in the path. Finding F14: the NIfTI
    # loader appends `?frame=N` with a literal `?`, so a URL that already carries
    # `?file_key=` produces two of them and every slice resolves to frame 0. The
    # maxillo CBCT display volume *is* a bundle member, so the viewer needs a
    # query-free way to name one. `filename` stays decorative here too.
    path(
        "api/processing/files/serve/<int:file_id>/key/<str:bundle_key>/<str:filename>",
        api_views.serve_file,
        name="api_serve_file_bundle",
    ),
    # Measurements made in the volume grid become durable annotation revisions.
    # Domain-oriented on purpose (the governing architectural rule): the URL names a
    # patient and the work, not a viewer. See annotations/views.py.
    path(
        "api/patients/<int:patient_id>/measurements/",
        annotations_views.save_measurements_api,
        name="api_save_measurements",
    ),
    path(
        "api/patients/<int:patient_id>/measurements/state/",
        annotations_views.measurements_state_api,
        name="api_measurements_state",
    ),
]
