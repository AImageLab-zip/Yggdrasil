"""Patient upload views (single patient, and admin bulk ingestion)."""
import logging

from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from common.activity import log_activity
from common.models import Project, ProjectAccess
from common.permissions import filter_folders_for_user, user_is_project_admin
from .domain import get_domain_forms, get_domain_models
from .helpers import bulk_upload_url_for, redirect_with_namespace

logger = logging.getLogger(__name__)


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
    
    # Per-project upload permission. `request.user.profile` is already bound to
    # the session's project, so the check above covers it; this one additionally
    # refuses a project the user has no access to at all.
    current_project_id = request.session.get('current_project_id')
    if current_project_id and not (
        user_is_project_admin(request.user, request)
        or ProjectAccess.objects.filter(
            user=request.user, project_id=current_project_id
        ).exists()
    ):
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

        upload_field_names = (
            {'video'} if namespace == 'laparoscopy' else
            {'cbct', 'ios_upper', 'ios_lower', 'teleradiography', 'panoramic', 'intraoral-photos'}
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
            #
            # Read from the project the form targets, not from the session: the
            # form lets the user pick a different project, and gating on the
            # session's project applied the wrong modality whitelist.
            target_project = patient_upload_form.cleaned_data.get('project') or current_project
            if target_project is not None and target_project != current_project:
                target_modality_slugs = set(
                    target_project.modalities.filter(is_active=True)
                    .exclude(slug='rawzip')
                    .values_list('slug', flat=True)
                )
            else:
                target_modality_slugs = allowed_modality_slugs

            def _mod_allowed(slug):
                return not target_modality_slugs or slug in target_modality_slugs

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
            
            # Handle CBCT
            cbct_file = request.FILES.get('cbct')
            cbct_error = None
            if cbct_file and _mod_allowed('cbct'):
                try:
                    modality = Modality.objects.get(slug='cbct')
                    patient.modalities.add(modality)

                    from ..file_utils import save_cbct_to_dataset

                    file_path, job = save_cbct_to_dataset(patient, cbct_file)
                    if file_path:
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
        # Admins can switch this screen into bulk mode; None hides the switch.
        'bulk_upload_url': bulk_upload_url_for(request, namespace),
    }
    return render(request, 'common/upload/upload.html', context)


# ---------------------------------------------------------------------------
# Bulk upload (administrators)
# ---------------------------------------------------------------------------
#
# One patient per uploaded file, named after the file, all landing in one folder
# of the current project. There is no Scan name field: the point of this screen is
# ingesting a directory of volumes without typing a name per case.
#
# The browser posts one file per request (each CBCT needs its own client-side
# conversion pass, and a single 200-volume request would be neither resumable nor
# reportable), but the view also accepts several files at once so the plain form
# works without JavaScript.

# Extensions that name a patient but must not end up in its name.
_KNOWN_UPLOAD_SUFFIXES = (
    '.nii.gz', '.tar.gz', '.nii', '.mha', '.mhd', '.nrrd', '.nhdr',
    '.dcm', '.dicom', '.stl', '.obj', '.ply', '.jpg', '.jpeg', '.png',
    '.zip', '.tar', '.tgz', '.7z', '.mp4', '.avi',
)


def patient_name_from_filename(filename):
    """Patient name for a bulk-uploaded file: its basename without the extension.

    Returns '' when nothing usable is left, in which case ``Patient.save()``
    falls back to ``Patient <id>``.
    """
    import os as _os

    base = _os.path.basename((filename or '').replace('\\', '/')).strip()
    lowered = base.lower()
    for suffix in _KNOWN_UPLOAD_SUFFIXES:
        if lowered.endswith(suffix):
            base = base[: -len(suffix)]
            break
    else:
        base = _os.path.splitext(base)[0]
    # Patient.name is a CharField(max_length=100).
    return base.strip()[:100]


def modality_for_filename(filename, modalities):
    """Resolve one modality for a bulk-uploaded file from its extension.

    Driven by ``Modality.supported_extensions`` and restricted to the project's
    own modalities, so a CBCT-only project never routes a file to IOS. Returns
    ``(modality, error)``: exactly one of the two is set. An extension shared by
    several of the project's modalities (e.g. .png across panoramic /
    teleradiography / intraoral photographs) is reported rather than guessed.
    """
    name = (filename or '').lower()
    matches = []
    for modality in modalities:
        extensions = modality.supported_extensions or []
        if any(name.endswith(str(extension).lower()) for extension in extensions):
            matches.append(modality)

    if not matches:
        return None, (
            'unsupported file type for this project '
            f"(enabled: {', '.join(sorted(m.slug for m in modalities)) or 'none'})"
        )
    if len(matches) > 1:
        return None, (
            'this file type is shared by several of the project\'s modalities '
            f"({', '.join(sorted(m.slug for m in matches))}); choose one explicitly"
        )
    return matches[0], None


def _save_bulk_file(patient, modality, uploaded_file):
    """Persist one bulk-uploaded file against a freshly created patient.

    Returns the created Job or None. Raises on failure so the caller can roll the
    patient row back.
    """
    from ..file_utils import (
        save_cbct_to_dataset,
        save_generic_modality_file,
        save_intraoral_photos_to_dataset,
    )

    # A patient per file cannot satisfy a modality that needs several files at
    # once (IOS is one case = upper + lower), so say so instead of half-creating it.
    if modality.requires_multiple_files and modality.slug == 'ios':
        raise ValueError(
            'IOS needs an upper and a lower scan for one patient; '
            'use the single-patient upload'
        )

    if modality.slug == 'cbct':
        _key, job = save_cbct_to_dataset(patient, uploaded_file)
        return job
    if modality.slug == 'intraoral-photo':
        saved, errors, job = save_intraoral_photos_to_dataset(patient, [uploaded_file])
        if not saved:
            raise ValueError(errors[0] if errors else 'the photograph could not be saved')
        return job
    if modality.slug == 'video':
        # Video has its own writer (subsampling, frame extraction), same as the
        # single-patient upload path.
        from laparoscopy.file_utils import save_video_to_dataset

        file_registry, job = save_video_to_dataset(patient, uploaded_file)
        if not file_registry:
            raise ValueError('the video could not be saved (storage may be unavailable)')
        return job
    _file_registry, job = save_generic_modality_file(patient, modality.slug, uploaded_file)
    return job


@login_required
@require_http_methods(['GET', 'POST'])
def bulk_upload_patients(request):
    """Create one patient per uploaded file, in one folder of the current project."""
    domain_models = get_domain_models(request)
    Patient = domain_models['Patient']
    Folder = domain_models['Folder']
    namespace = (getattr(request, 'resolver_match', None) and request.resolver_match.namespace) or 'maxillo'

    current_project_id = request.session.get('current_project_id')
    current_project = None
    if current_project_id:
        current_project = Project.objects.prefetch_related('modalities').filter(
            id=current_project_id
        ).first()

    # Bulk ingestion creates patients wholesale and bypasses per-case review, so
    # it is administrators only (the single-patient upload stays open to annotators).
    if not current_project or not user_is_project_admin(request.user, current_project):
        message = 'Bulk upload is restricted to project administrators.'
        if _wants_json(request):
            return JsonResponse({'ok': False, 'error': message}, status=403)
        messages.error(request, message)
        return redirect_with_namespace(request, 'patient_list')

    allowed_modalities = [
        modality
        for modality in current_project.modalities.filter(is_active=True).order_by('name')
        if modality.slug != 'rawzip'
    ]
    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(project_id=current_project.id).order_by('name'),
        namespace,
    )

    if request.method == 'GET':
        return render(request, 'common/upload/bulk_upload.html', {
            'current_project': current_project,
            'folders': folders,
            'allowed_modalities': allowed_modalities,
            'accept_attribute': ','.join(sorted({
                str(extension)
                for modality in allowed_modalities
                for extension in (modality.supported_extensions or [])
            })),
        })

    uploaded_files = request.FILES.getlist('files')
    if not uploaded_files:
        return _bulk_response(request, [], 'Select at least one file to upload.')

    folder = None
    folder_id = request.POST.get('folder')
    if folder_id:
        folder = next((f for f in folders if str(f.id) == str(folder_id)), None)
    if folder is None:
        return _bulk_response(
            request, [], 'Choose a folder of this project to upload into.'
        )

    forced_modality = None
    forced_slug = (request.POST.get('modality') or '').strip()
    if forced_slug:
        forced_modality = next((m for m in allowed_modalities if m.slug == forced_slug), None)
        if forced_modality is None:
            return _bulk_response(
                request, [], 'The selected modality is not enabled for this project.'
            )

    results = []
    for uploaded_file in uploaded_files:
        results.append(
            _bulk_upload_one(
                request, Patient, current_project, folder, forced_modality,
                allowed_modalities, uploaded_file,
            )
        )
    return _bulk_response(request, results)


def _bulk_upload_one(
    request, Patient, project, folder, forced_modality, allowed_modalities, uploaded_file
):
    """Create one patient for one file. Never raises; reports per-file outcomes."""
    filename = getattr(uploaded_file, 'name', '') or 'file'
    modality = forced_modality
    if modality is None:
        modality, error = modality_for_filename(filename, allowed_modalities)
        if modality is None:
            return {'file': filename, 'ok': False, 'error': error}

    # One transaction per file: a file that fails validation leaves no patient
    # behind, and does not abort the rest of the batch.
    try:
        with transaction.atomic():
            patient = Patient(
                name=patient_name_from_filename(filename),
                project=project,
                folder=folder,
                uploaded_by=request.user,
            )
            patient.save()
            patient.modalities.add(modality)
            job = _save_bulk_file(patient, modality, uploaded_file)
    except Exception as exc:  # noqa: BLE001 - reported per file, batch continues
        detail = getattr(exc, 'message', None) or (
            '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc)
        )
        logger.warning('Bulk upload failed for %s: %s', filename, detail)
        return {'file': filename, 'ok': False, 'error': detail}

    log_activity(
        request.user,
        (getattr(request, 'resolver_match', None) and request.resolver_match.namespace) or 'maxillo',
        patient.patient_id,
        patient_name=patient.name,
        verb='uploaded',
        target=modality.slug,
        bulk=True,
        original_filename=filename,
    )
    return {
        'file': filename,
        'ok': True,
        'patient_id': patient.patient_id,
        'patient_name': patient.name,
        'modality': modality.slug,
        'job_id': job.id if job else None,
    }


def _wants_json(request):
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _bulk_response(request, results, error=None):
    """JSON for the XHR uploader, messages + redirect for the plain form."""
    created = [item for item in results if item.get('ok')]
    failed = [item for item in results if not item.get('ok')]

    if _wants_json(request):
        if error:
            return JsonResponse({'ok': False, 'error': error, 'results': results}, status=400)
        return JsonResponse(
            {
                'ok': not failed,
                'created': len(created),
                'failed': len(failed),
                'results': results,
            },
            status=200 if created or not results else 400,
        )

    if error:
        messages.error(request, error)
        return redirect_with_namespace(request, 'bulk_upload_patients')
    if created:
        messages.success(request, f'Created {len(created)} patient(s).')
    for item in failed:
        messages.error(request, f"{item['file']}: {item['error']}")
    if created and not failed:
        return redirect_with_namespace(request, 'patient_list')
    return redirect_with_namespace(request, 'bulk_upload_patients')
