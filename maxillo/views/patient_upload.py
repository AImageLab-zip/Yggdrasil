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

    current_project = None
    allowed_modalities = []
    if current_project_id:
        try:
            current_project = Project.objects.prefetch_related('modalities').get(id=current_project_id)
            allowed_modalities = list(current_project.modalities.filter(is_active=True).exclude(slug='rawzip'))
        except Project.DoesNotExist:
            pass
    allowed_modality_slugs = {m.slug for m in allowed_modalities}

    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(parent__isnull=True, project_id=current_project_id).order_by('name') if current_project_id else Folder.objects.none(),
        namespace,
    )
    
    if request.method == 'POST':
        patient_upload_form = PatientUploadForm(request.POST, request.FILES, user=request.user, current_project=current_project, domain=namespace)
        patient_form = PatientForm()

        # Validate CBCT folder uploads before creating the patient so invalid
        # folder selections do not leave behind empty patient rows.
        cbct_upload_type = request.POST.get('cbct_upload_type', 'file')
        cbct_folder_files = request.FILES.getlist('cbct_folder_files')
        if cbct_upload_type == 'folder' and cbct_folder_files:
            try:
                from ..models import validate_cbct_folder

                validate_cbct_folder(cbct_folder_files)
            except Exception as e:
                messages.error(request, f'Error validating CBCT folder: {e}')
                allowed_folders = filter_folders_for_user(
                    request.user,
                    Folder.objects.filter(parent__isnull=True).order_by('name'),
                    namespace,
                )
                return render(request, 'common/upload/upload.html', {
                    'patient_form': patient_form,
                    'patient_upload_form': patient_upload_form,
                    'folders': allowed_folders,
                    'allowed_modalities': allowed_modalities,
                })

        upload_field_names = (
            {'video'} if namespace == 'laparoscopy' else
            {'cbct', 'cbct_folder_files', 'ios_upper', 'ios_lower', 'teleradiography', 'panoramic', 'intraoral-photos'}
        )
        has_upload = any(request.FILES.getlist(field_name) for field_name in upload_field_names)
        form_is_valid = patient_upload_form.is_valid()
        if form_is_valid and not has_upload:
            patient_upload_form.add_error(None, 'Add at least one file before uploading.')
            form_is_valid = False
        if form_is_valid and len(request.FILES.getlist('intraoral-photos')) > 10:
            patient_upload_form.add_error(None, 'Select no more than 10 intraoral photographs.')
            form_is_valid = False

        if form_is_valid:
            # Create and populate Patient from the form
            patient = patient_upload_form.save(commit=False)
            patient.uploaded_by = request.user

            # Project scoping: a modality the project does not enable is never
            # saved even if the client POSTs it (defense in depth; the UI only
            # renders enabled modalities). Empty project config keeps the
            # historical permissive behavior.
            def _mod_allowed(slug):
                return not allowed_modality_slugs or slug in allowed_modality_slugs

            # Project + folder are assigned by the form (both mandatory).
            patient.folder = patient_upload_form.cleaned_data.get('folder')
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
            cbct_error = None
            if (cbct_file or cbct_folder_files) and _mod_allowed('cbct'):
                try:
                    modality = Modality.objects.get(slug='cbct')
                    patient.modalities.add(modality)
                    
                    if cbct_file:
                        from ..file_utils import save_cbct_to_dataset

                        file_path, job = save_cbct_to_dataset(patient, cbct_file)
                        if file_path:
                            uploaded_modalities.append('CBCT')
                            if job:
                                processing_job_ids.append(job.id)
                    elif cbct_folder_files:
                        from ..file_utils import save_cbct_folder_to_dataset

                        folder_path, job = save_cbct_folder_to_dataset(patient, cbct_folder_files)
                        if folder_path:
                            uploaded_modalities.append('CBCT')
                            if job:
                                processing_job_ids.append(job.id)
                except Exception as e:
                    err_text = str(e)
                    if hasattr(e, 'message') and e.message:
                        err_text = e.message
                    elif hasattr(e, 'messages') and e.messages:
                        err_text = '; '.join(e.messages)
                    cbct_error = f"Error saving CBCT: {err_text}"
                    messages.error(request, cbct_error)

            # Handle IOS (upper + lower)
            ios_upper = request.FILES.get('ios_upper')
            ios_lower = request.FILES.get('ios_lower')
            if (ios_upper and ios_lower) and _mod_allowed('ios'):
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

            # Handle single-file generic modalities (Teleradiography, Panoramic)
            from ..file_utils import save_generic_modality_file
            for slug, label in (('teleradiography', 'Teleradiography'), ('panoramic', 'Panoramic')):
                generic_file = request.FILES.get(slug)
                if generic_file and _mod_allowed(slug):
                    try:
                        modality = Modality.objects.get(slug=slug)
                        patient.modalities.add(modality)

                        fr, job = save_generic_modality_file(patient, slug, generic_file)
                        if fr:
                            uploaded_modalities.append(label)
                            if job:
                                processing_job_ids.append(job.id)
                    except Exception as e:
                        messages.error(request, f"Error saving {label}: {e}")

            # Handle Intraoral Photos (multiple files)
            intraoral_photos = request.FILES.getlist('intraoral-photos')
            if intraoral_photos and _mod_allowed('intraoral-photo'):
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
            if video_file and _mod_allowed('video'):
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

            if is_xhr:
                if cbct_error:
                    return JsonResponse({'ok': False, 'error': cbct_error}, status=400)
                if video_error:
                    return JsonResponse({'ok': False, 'error': video_error}, status=400)

            if video_error:
                messages.error(request, video_error)


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
        patient_upload_form = PatientUploadForm(user=request.user, current_project=current_project, domain=namespace)
    
    context = {
        'patient_form': patient_form,
        'patient_upload_form': patient_upload_form,
        'folders': folders,
        'allowed_modalities': allowed_modalities,
    }
    return render(request, 'common/upload/upload.html', context)
