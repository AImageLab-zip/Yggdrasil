from django.urls import path
from django.shortcuts import redirect

app_name = "maxillo"
from . import views
from . import api_views

urlpatterns = [
    path("", views.home, name="home"),
    path("patients/", views.patient_list, name="patient_list"),
    path("upload/", views.upload_patient, name="upload_patient"),
    path(
        "project/<int:project_id>/select/", views.select_project, name="select_project"
    ),
    path("patient/<int:patient_id>/", views.patient_detail, name="patient_detail"),
    path(
        "patient/<int:patient_id>/update/",
        views.update_classification,
        name="update_classification",
    ),
    path(
        "patient/<int:patient_id>/update-name/",
        views.update_patient_name,
        name="update_patient_name",
    ),
    path(
        "patient/<int:patient_id>/files/raw/add/",
        views.add_raw_file,
        name="add_raw_file",
    ),
    path(
        "patient/<int:patient_id>/files/raw/<int:file_id>/delete/",
        views.delete_raw_file,
        name="delete_raw_file",
    ),
    path(
        "patient/<int:patient_id>/voice-caption/",
        views.upload_voice_caption,
        name="upload_voice_caption",
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
    # Admin endpoints
    path(
        "patient/<int:patient_id>/delete/", views.delete_patient, name="delete_patient"
    ),
    path(
        "patients/bulk-delete/", views.bulk_delete_patients, name="bulk_delete_patients"
    ),
    path(
        "patients/bulk-rerun-processing/",
        views.bulk_rerun_processing,
        name="bulk_rerun_processing",
    ),
    path(
        "patient/<int:patient_id>/rerun-processing/",
        views.rerun_processing,
        name="rerun_processing",
    ),
    path(
        "admin/control-panel/",
        lambda request: redirect("admin_control_panel"),
        name="admin_control_panel",
    ),
    # Profile
    path("profile/", views.user_profile, name="user_profile"),
    path(
        "profile/<str:username>/", views.user_profile, name="user_profile_by_username"
    ),
    # Folder/tag management
    path("folders/create/", views.create_folder, name="create_folder"),
    path("folders/<int:folder_id>/stats/", views.folder_stats, name="folder_stats"),
    path("folders/<int:folder_id>/rename/", views.rename_folder, name="rename_folder"),
    path("folders/<int:folder_id>/permissions/", views.folder_permissions, name="folder_permissions"),
    path("folders/<int:folder_id>/permissions/upsert/", views.upsert_folder_permission, name="upsert_folder_permission"),
    path("folders/<int:folder_id>/permissions/<int:user_id>/delete/", views.delete_folder_permission, name="delete_folder_permission"),
    path(
        "folders/move-patients/",
        views.move_patients_to_folder,
        name="move_patients_to_folder",
    ),
    # Export endpoints
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
    path("export/<int:export_id>/stop/", views.export_stop, name="export_stop"),
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
    # API endpoints
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
    path(
        "api/patient/<int:patient_id>/intraoral-segmentation/",
        views.patient_intraoral_segmentation_data,
        name="patient_intraoral_segmentation_data",
    ),
    path(
        "api/patient/<int:patient_id>/intraoral-segmentation/update/",
        views.update_patient_intraoral_segmentation,
        name="update_patient_intraoral_segmentation",
    ),
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
    # API mirror endpoints under /maxillo/api/*
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
    path(
        "api/runner/jobs/<int:job_id>/claim/",
        api_views.runner_claim_job,
        name="api_runner_claim_job",
    ),
    path(
        "api/runner/jobs/<int:job_id>/complete/",
        api_views.runner_complete_job,
        name="api_runner_complete_job",
    ),
    path(
        "api/runner/jobs/<int:job_id>/fail/",
        api_views.runner_fail_job,
        name="api_runner_fail_job",
    ),
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
]
