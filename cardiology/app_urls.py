from django.shortcuts import redirect
from django.urls import path

from annotations import views as annotations_views
from cardiology import views
from common.domain_views import deletion as shared_deletion
from common.domain_views import export as shared_export
from common.domain_views import folders_tags as shared_folders
from common.domain_views import profile as shared_profile
from common.domain_views import voice_captions as shared_captions

app_name = "cardiology"

urlpatterns = [
    path("", views.home, name="home"),
    path("patients/", views.patient_list, name="patient_list"),
    path("upload/", views.upload_patient, name="upload_patient"),
    path("upload/bulk/", views.bulk_upload_patients, name="bulk_upload_patients"),
    path("project/<int:project_id>/select/", views.select_project, name="select_project"),
    path("patient/<int:patient_id>/", views.patient_detail, name="patient_detail"),
    path("patient/<int:patient_id>/update-name/", views.update_patient_name, name="update_patient_name"),
    path("patient/<int:patient_id>/delete/", shared_deletion.delete_patient, name="delete_patient"),
    path("patients/bulk-delete/", shared_deletion.bulk_delete_patients, name="bulk_delete_patients"),
    path("patient/<int:patient_id>/rerun-processing/", views.rerun_processing, name="rerun_processing"),
    path("patients/bulk-rerun-processing/", views.bulk_rerun_processing, name="bulk_rerun_processing"),

    # Text notes (voice captions)
    path("patient/<int:patient_id>/text-caption/", shared_captions.upload_text_caption, name="upload_text_caption"),
    path(
        "patient/<int:patient_id>/voice-caption/<int:caption_id>/delete/",
        shared_captions.delete_voice_caption,
        name="delete_voice_caption",
    ),
    path(
        "patient/<int:patient_id>/voice-caption/<int:caption_id>/edit/",
        shared_captions.edit_voice_caption_transcription,
        name="edit_voice_caption_transcription",
    ),
    path(
        "patient/<int:patient_id>/voice-caption/<int:caption_id>/update-modality/",
        shared_captions.update_voice_caption_modality,
        name="update_voice_caption_modality",
    ),

    # The AF/NSR/Other/NI rhythm call
    path("patient/<int:patient_id>/classification/", views.classification_update, name="classification_update"),

    # Files and the browser-generated plot
    path("api/processing/files/serve/<int:file_id>/", views.serve_file, name="api_serve_file"),
    path("api/processing/files/serve/<int:file_id>/<str:filename>", views.serve_file, name="api_serve_file_named"),
    path("api/patient/<int:patient_id>/ecg/generated/", views.save_browser_ecg_plot, name="save_browser_ecg_plot"),
    path("ecg/warmup/", views.ecg_warmup, name="ecg_warmup"),
    path("api/ecg/warmup/pending/", views.ecg_warmup_pending, name="ecg_warmup_pending"),

    # Measurements (shared annotation API every domain routes)
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

    # Tags
    path("patient/<int:patient_id>/tags/add/", shared_folders.add_patient_tag, name="add_patient_tag"),
    path("patient/<int:patient_id>/tags/remove/", shared_folders.remove_patient_tag, name="remove_patient_tag"),

    # Folders
    path("folders/create/", shared_folders.create_folder, name="create_folder"),
    path("folders/<int:folder_id>/stats/", shared_folders.folder_stats, name="folder_stats"),
    path("folders/<int:folder_id>/rename/", shared_folders.rename_folder, name="rename_folder"),
    path("folders/<int:folder_id>/delete/", shared_folders.delete_folder, name="delete_folder"),
    path("folders/move-patients/", shared_folders.move_patients_to_folder, name="move_patients_to_folder"),

    # Profile
    path("profile/", shared_profile.user_profile, name="user_profile"),
    path("profile/<str:username>/", shared_profile.user_profile, name="user_profile_by_username"),
    path("admin/control-panel/", lambda request: redirect("admin_control_panel"), name="admin_control_panel"),

    # Export
    path("export/", shared_export.export_list, name="export_list"),
    path("export/new/", shared_export.export_new, name="export_new"),
    path("export/preview/", shared_export.export_preview, name="export_preview"),
    path("export/<int:export_id>/", shared_export.export_status, name="export_status"),
    path("export/<int:export_id>/download/", shared_export.export_download, name="export_download"),
    path("export/<int:export_id>/share/", shared_export.export_share_update, name="export_share_update"),
    path("export/<int:export_id>/stop/", shared_export.export_stop, name="export_stop"),
    path("export/shared/<str:share_token>/", shared_export.export_shared_landing, name="export_shared_landing"),
    path(
        "export/shared/<str:share_token>/download/",
        shared_export.export_shared_download,
        name="export_shared_download",
    ),
    path("export/<int:export_id>/delete/", shared_export.export_delete, name="export_delete"),
]
