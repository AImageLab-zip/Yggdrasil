from django.shortcuts import redirect
from django.urls import path

from annotations import views as annotations_views
from urology import views, wsi_views

app_name = "urology"

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
        "patients/bulk-upload/",
        views.bulk_upload_patients,
        name="bulk_upload_patients",
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
        "api/processing/files/serve/<int:file_id>/",
        wsi_views.serve_file,
        name="api_serve_file",
    ),
    path(
        "api/processing/files/serve/<int:file_id>/<str:filename>",
        wsi_views.serve_file,
        name="api_serve_file_named",
    ),
    path(
        "api/wsi/<int:file_id>/metadata/",
        wsi_views.wsi_metadata_api,
        name="wsi_metadata",
    ),
    path(
        "api/wsi/<int:file_id>/tile/<int:level>/<int:col>_<int:row>.png",
        wsi_views.wsi_tile_api,
        name="wsi_tile",
    ),
    path(
        "api/wsi/<int:file_id>/tile/<int:level>/<int:col>_<int:row>.jpg",
        wsi_views.wsi_tile_api,
        name="wsi_tile_jpg",
    ),
    path(
        "api/wsi/<int:file_id>/tile/<int:level>/<int:col>_<int:row>.jpeg",
        wsi_views.wsi_tile_api,
        name="wsi_tile_jpeg",
    ),
    path(
        "api/wsi/<int:file_id>/thumbnail/",
        wsi_views.wsi_thumbnail_api,
        name="wsi_thumbnail",
    ),
    path(
        "api/wsi/<int:file_id>/segmentation/",
        wsi_views.wsi_segmentation_api,
        name="wsi_segmentation",
    ),
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
