"""Patient upload view."""
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse

from common.models import Project
from common.permissions import filter_folders_for_user, user_is_project_admin
from .domain import get_domain_forms, get_domain_models
from .helpers import redirect_with_namespace


@login_required
def upload_patient(request):
    user_profile = request.user.profile
    domain_models = get_domain_models(request)
    domain_forms = get_domain_forms(request)

    PatientForm = domain_forms['PatientForm']
    PatientUploadForm = domain_forms['PatientUploadForm']
    Folder = domain_models['Folder']
    namespace = (getattr(request, 'resolver_match', None) and request.resolver_match.namespace) or 'maxillo'
    
    if not request.user.profile:
        messages.error(request, 'You do not have permission to upload scans.')
        return redirect_with_namespace(request, 'patient_list')

    if not user_profile.can_upload_scans():
        messages.error(request, 'You do not have permission to upload scans.')
        return redirect_with_namespace(request, 'patient_list')
    
    # Enforce per-project upload permission
    current_project_id = request.session.get('current_project_id')
    if current_project_id and not user_profile.can_upload_scans():
        messages.error(request, 'You are not allowed to upload in this project.')
        return redirect_with_namespace(request, 'patient_list')
    
    if request.method == 'POST':
        patient_upload_form = PatientUploadForm(request.POST, request.FILES, user=request.user)
        patient_form = PatientForm()

        # For now, we do not support CBCT folder uploads
        cbct_upload_type = request.POST.get('cbct_upload_type', 'file')
        if cbct_upload_type == 'folder' and request.FILES.getlist('cbct_folder_files'):
            messages.error(request, 'CBCT Folder uploads have been disabled.')
            allowed_folders = filter_folders_for_user(
                request.user,
                Folder.objects.filter(parent__isnull=True).order_by('name'),
                namespace,
            )
            return render(request, 'common/upload/upload.html', {
                'patient_form': patient_form,
                'patient_upload_form': patient_upload_form,
                'folders': allowed_folders,
            })

        if patient_upload_form.is_valid():
            # Create and populate Patient from the form
            patient = patient_upload_form.save(commit=False)
            patient.uploaded_by = request.user
            
            # Assign folder if provided
            folder = patient_upload_form.cleaned_data.get('folder')
            if folder:
                allowed_folder_ids = set(
                    filter_folders_for_user(
                        request.user,
                        Folder.objects.filter(parent__isnull=True).only('id'),
                        namespace,
                    ).values_list('id', flat=True)
                )
                if folder.id not in allowed_folder_ids:
                    messages.error(request, 'You do not have permission to upload to the selected folder.')
                    allowed_folders = filter_folders_for_user(
                        request.user,
                        Folder.objects.filter(parent__isnull=True).order_by('name'),
                        namespace,
                    )
                    return render(request, 'common/upload/upload.html', {
                        'patient_form': patient_form,
                        'patient_upload_form': patient_upload_form,
                        'folders': allowed_folders,
                    })
                patient.folder = folder
            patient.save()

            # The form's save() handles tags
            patient_upload_form.instance = patient
            patient_upload_form.save(commit=True)

            # Add modalities to patient
            from common.models import Modality

            uploaded_modalities = []
            processing_job_ids = []
            bite_job_ids = []
            
            # Handle CBCT (single file or folder)
            cbct_file = request.FILES.get('cbct')
            cbct_folder_files = request.FILES.getlist('cbct_folder_files')
            if cbct_file or cbct_folder_files:
                try:
                    modality = Modality.objects.get(slug='cbct')
                    patient.modalities.add(modality)
                    
                    if cbct_file:
                        from ..file_utils import save_generic_modality_file
                        fr, job = save_generic_modality_file(patient, 'cbct', cbct_file)
                        if fr:
                            uploaded_modalities.append('CBCT')
                            if job:
                                processing_job_ids.append(job.id)
                    elif cbct_folder_files:
                        from ..file_utils import save_generic_modality_folder
                        fr, job = save_generic_modality_folder(patient, 'cbct', cbct_folder_files)
                        if fr:
                            uploaded_modalities.append('CBCT')
                            if job:
                                processing_job_ids.append(job.id)
                except Exception as e:
                    messages.error(request, f"Error saving CBCT: {e}")

            # Handle IOS (upper + lower)
            ios_upper = request.FILES.get('ios_upper')
            ios_lower = request.FILES.get('ios_lower')
            if ios_upper and ios_lower:
                try:
                    modality = Modality.objects.get(slug='ios')
                    patient.modalities.add(modality)
                    
                    from ..file_utils import save_ios_to_dataset
                    ios_result = save_ios_to_dataset(patient, ios_upper, ios_lower)
                    uploaded_modalities.append('IOS')
                    if ios_result.get('processing_job'):
                        processing_job_ids.append(ios_result['processing_job'].id)
                    if ios_result.get('bite_classification_job'):
                        bite_job_ids.append(ios_result['bite_classification_job'].id)
                except Exception as e:
                    messages.error(request, f"Error saving IOS: {e}")

            # Handle Teleradiography
            teleradiography_file = request.FILES.get('teleradiography')
            if teleradiography_file:
                try:
                    modality = Modality.objects.get(slug='teleradiography')
                    patient.modalities.add(modality)
                    
                    from ..file_utils import save_generic_modality_file
                    fr, job = save_generic_modality_file(patient, 'teleradiography', teleradiography_file)
                    if fr:
                        uploaded_modalities.append('Teleradiography')
                        if job:
                            processing_job_ids.append(job.id)
                except Exception as e:
                    messages.error(request, f"Error saving Teleradiography: {e}")

            # Handle Panoramic
            panoramic_file = request.FILES.get('panoramic')
            if panoramic_file:
                try:
                    modality = Modality.objects.get(slug='panoramic')
                    patient.modalities.add(modality)
                    
                    from ..file_utils import save_generic_modality_file
                    fr, job = save_generic_modality_file(patient, 'panoramic', panoramic_file)
                    if fr:
                        uploaded_modalities.append('Panoramic')
                        if job:
                            processing_job_ids.append(job.id)
                except Exception as e:
                    messages.error(request, f"Error saving Panoramic: {e}")

            # Handle Intraoral Photos (multiple files)
            intraoral_photos = request.FILES.getlist('intraoral-photos')
            if intraoral_photos:
                try:
                    modality = Modality.objects.get(slug='intraoral-photo')
                    patient.modalities.add(modality)

                    if len(intraoral_photos) > 10:
                        messages.warning(request, f"Too many intraoral images ({len(intraoral_photos)}). Only first 10 will be processed.")
                        intraoral_photos = intraoral_photos[:10]

                    from ..file_utils import save_intraoral_photos_to_dataset
                    saved, errors, job = save_intraoral_photos_to_dataset(patient, intraoral_photos)
                    if saved:
                        uploaded_modalities.append(f'Intraoral Photos ({len(saved)})')
                        if job:
                            processing_job_ids.append(job.id)
                    if errors:
                        messages.warning(request, f"{len(errors)} intraoral photo(s) failed to upload")
                except Exception as e:
                    messages.error(request, f"Error saving Intraoral Photos: {e}")


            # Generic video modality
            video_file = request.FILES.get('video')
            video_error = None
            if video_file:
                try:
                    modality = Modality.objects.get(slug='video')
                    patient.modalities.add(modality)

                    from laparoscopy.file_utils import save_video_to_dataset
                    fr, job = save_video_to_dataset(patient, video_file)
                    if fr:
                        uploaded_modalities.append('Video')
                        if job:
                            processing_job_ids.append(job.id)
                    else:
                        video_error = 'Video file could not be saved (storage may be unavailable).'
                except Exception as e:
                    video_error = f"Error saving Video: {e}"

            is_xhr = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

            if video_error:
                messages.error(request, video_error)
                if is_xhr:
                    return JsonResponse({'ok': False, 'error': video_error}, status=400)


            if uploaded_modalities:
                unique_modalities = list(dict.fromkeys(uploaded_modalities))
                summary_message = (
                    f"Patient uploaded successfully with {len(unique_modalities)} modality(s): "
                    f"{', '.join(unique_modalities)}."
                )
                if processing_job_ids:
                    summary_message += f" Processing jobs: #{', #'.join(str(job_id) for job_id in processing_job_ids)}."
                if bite_job_ids:
                    summary_message += f" Bite classification jobs: #{', #'.join(str(job_id) for job_id in bite_job_ids)}."
                messages.success(request, summary_message)
            else:
                messages.success(request, 'Patient uploaded successfully!')

            if is_xhr:
                from django.urls import reverse, NoReverseMatch
                ns = (getattr(request, 'resolver_match', None) and request.resolver_match.namespace) or 'maxillo'
                try:
                    redirect_url = reverse(f"{ns}:patient_list")
                except NoReverseMatch:
                    redirect_url = reverse('maxillo:patient_list')
                return JsonResponse({'ok': True, 'redirect': redirect_url})

            return redirect_with_namespace(request, 'patient_list')
    else:
        patient_form = PatientForm()
        patient_upload_form = PatientUploadForm(user=request.user)
    
    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(parent__isnull=True).order_by('name'),
        namespace,
    )
    
    # Get allowed modalities for template rendering
    allowed_modalities = []
    cp_id = request.session.get('current_project_id')
    if cp_id:
        try:
            proj = Project.objects.prefetch_related('modalities').get(id=cp_id)
            allowed_modalities = list(proj.modalities.filter(is_active=True))
        except Project.DoesNotExist:
            pass
    
    context = {
        'patient_form': patient_form,
        'patient_upload_form': patient_upload_form,
        'folders': folders,
        'allowed_modalities': allowed_modalities,
    }
    return render(request, 'common/upload/upload.html', context)
