from django.urls import path
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required

from common.models import Project
from common.permissions import entry_project_for
from . import views


app_name = "cardiology"


@login_required
def set_cardiology(request):
    proj = entry_project_for(request.user, 'cardiology')
    if proj is None:
        proj = Project.objects.filter(slug='cardiology').first()
    if proj is None:
        proj = Project.objects.create(name='Cardiology', slug='cardiology', domain='cardiology')

    request.session['current_project_id'] = proj.id
    return redirect('cardiology:patient_list')


urlpatterns = [
    path('', set_cardiology, name='cardiology_home'),
    path('patients/', views.patient_list, name='patient_list'),
    path('upload/', views.upload_patient, name='upload_patient'),
    path('upload/bulk/', views.bulk_upload_patients, name='bulk_upload_patients'),
    path('project/<int:project_id>/select/', views.select_project, name='select_project'),
    path('patient/<int:patient_id>/', views.patient_detail, name='patient_detail'),
    path('patient/<int:patient_id>/update-name/', views.update_patient_name, name='update_patient_name'),
    path('patient/<int:patient_id>/delete/', views.delete_patient, name='delete_patient'),
    path('patients/bulk-delete/', views.bulk_delete_patients, name='bulk_delete_patients'),

    # Text notes (voice captions)
    path('patient/<int:patient_id>/text-caption/', views.upload_text_caption, name='upload_text_caption'),
    path('patient/<int:patient_id>/voice-caption/<int:caption_id>/delete/', views.delete_voice_caption, name='delete_voice_caption'),
    path('patient/<int:patient_id>/voice-caption/<int:caption_id>/edit/', views.edit_voice_caption_transcription, name='edit_voice_caption_transcription'),
    path('patient/<int:patient_id>/voice-caption/<int:caption_id>/update-modality/', views.update_voice_caption_modality, name='update_voice_caption_modality'),

    # Categorical AF/NSR/Other/NI call
    path('patient/<int:patient_id>/classification/', views.classification_update, name='classification_update'),

    # ECG file
    path('api/patient/<int:patient_id>/ecg/<int:file_id>/', views.serve_ecg_file, name='serve_ecg_file'),
    path('api/patient/<int:patient_id>/ecg/generated/', views.save_browser_ecg_plot, name='save_browser_ecg_plot'),
    path('ecg/warmup/', views.ecg_warmup, name='ecg_warmup'),
    path('api/ecg/warmup/pending/', views.ecg_warmup_pending, name='ecg_warmup_pending'),

    # Tags
    path('patient/<int:patient_id>/tags/add/', views.add_patient_tag, name='add_patient_tag'),
    path('patient/<int:patient_id>/tags/remove/', views.remove_patient_tag, name='remove_patient_tag'),

    # Folders
    path('folders/create/', views.create_folder, name='create_folder'),
    path('folders/<int:folder_id>/stats/', views.folder_stats, name='folder_stats'),
    path('folders/<int:folder_id>/rename/', views.rename_folder, name='rename_folder'),
    path('folders/<int:folder_id>/delete/', views.delete_folder, name='delete_folder'),
    path('folders/move-patients/', views.move_patients_to_folder, name='move_patients_to_folder'),

    # Profile
    path('profile/', views.user_profile, name='user_profile'),
    path('profile/<str:username>/', views.user_profile, name='user_profile_by_username'),

    # Export
    path('export/', views.export_list, name='export_list'),
    path('export/new/', views.export_new, name='export_new'),
    path('export/preview/', views.export_preview, name='export_preview'),
    path('export/<int:export_id>/', views.export_status, name='export_status'),
    path('export/<int:export_id>/download/', views.export_download, name='export_download'),
    path('export/<int:export_id>/share/', views.export_share_update, name='export_share_update'),
    path('export/<int:export_id>/stop/', views.export_stop, name='export_stop'),
    path('export/shared/<str:share_token>/', views.export_shared_landing, name='export_shared_landing'),
    path('export/shared/<str:share_token>/download/', views.export_shared_download, name='export_shared_download'),
    path('export/<int:export_id>/delete/', views.export_delete, name='export_delete'),
]
