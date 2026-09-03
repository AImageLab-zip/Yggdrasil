"""Voice caption management views."""
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import JsonResponse
import json
import logging
from common.permissions import (
    project_allows_annotation,
    user_can_delete_caption,
    user_can_edit_caption,
    user_can_write_annotations,
    user_is_project_admin,
)

from .domain import get_domain_models

logger = logging.getLogger(__name__)


@login_required
def delete_voice_caption(request, patient_id, caption_id):
    if request.method != 'DELETE':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    domain_models = get_domain_models(request)
    Patient = domain_models['Patient']
    VoiceCaption = domain_models['VoiceCaption']
    
    patient = get_object_or_404(Patient, patient_id=patient_id)

    if not (user_is_project_admin(request.user, request) or (patient.folder and user_can_write_annotations(request.user, patient.folder, request))):
        return JsonResponse({'error': 'Permission denied'}, status=403)
    voice_caption = get_object_or_404(VoiceCaption, id=caption_id, patient=patient)
    
    # Check permissions
    is_owner = voice_caption.user == request.user
    is_admin = user_is_project_admin(request.user, request)
    
    # If not owner and not admin, deny access
    if not is_owner and not is_admin:
        return JsonResponse({
            'error': 'You cannot delete voice captions created by other users.',
            'code': 'not_owner'
        }, status=403)
    
    # If admin is deleting someone else's caption, require confirmation
    if is_admin and not is_owner:
        # Check if this is a confirmation request
        data = json.loads(request.body) if request.body else {}
        if not data.get('admin_confirmed'):
            return JsonResponse({
                'error': 'Admin confirmation required',
                'code': 'admin_confirmation_required',
                'message': f'You are about to delete a voice caption created by {voice_caption.user.username}. Please confirm this action.'
            }, status=403)
    
    try:
        # Delete the audio file from FileRegistry if it exists
        audio_file = voice_caption.get_audio_file()
        if audio_file:
            audio_file.delete()
        
        # Delete the caption
        voice_caption.delete()
        
        return JsonResponse({'success': True})
        
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def upload_text_caption(request, patient_id):
    """Handle text caption submission (alternative to voice recording)"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    domain_models = get_domain_models(request)
    Patient = domain_models['Patient']
    VoiceCaption = domain_models['VoiceCaption']
    
    patient = get_object_or_404(Patient, patient_id=patient_id)

    if not (user_is_project_admin(request.user, request) or (patient.folder and user_can_write_annotations(request.user, patient.folder, request))):
        return JsonResponse({'error': 'Permission denied'}, status=403)
    if not project_allows_annotation(patient, 'voice_caption'):
        return JsonResponse({'error': 'Voice captions are disabled for this project'}, status=403)
    
    # Check permissions
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)
    
    try:
        data = json.loads(request.body)
        text_content = data.get('text', '').strip()
        modality = data.get('modality', '').strip()
        
        # Validate modality against database
        from ..modality_helpers import is_valid_modality_slug, get_all_modalities
        if not modality or not is_valid_modality_slug(modality):
            # Fallback to first available modality
            all_modalities = get_all_modalities()
            modality = all_modalities[0].slug if all_modalities else 'unknown'
        
        if not text_content:
            return JsonResponse({'error': 'Text content cannot be empty'}, status=400)

        
        # Create VoiceCaption instance for text-only caption
        voice_caption = VoiceCaption.objects.create(
            patient=patient,
            user=request.user,
            modality=modality,
            duration=0.0,  # No duration for text captions
            text_caption=text_content,
            original_text_caption=text_content,
            processing_status='completed',  # Text is already processed
            is_edited=False
        )

        # Activity feed (best-effort).
        from common.activity import log_activity
        _ns = request.resolver_match.namespace if request.resolver_match else 'maxillo'
        log_activity(request.user, _ns, patient.patient_id,
                     getattr(patient, 'name', ''), verb='captioned',
                     target='added a text caption')

        # Return caption data for the UI
        quality_status = voice_caption.get_quality_status()
        
        return JsonResponse({
            'success': True,
            'caption': {
                'id': voice_caption.id,
                'user_username': voice_caption.user.username,
                'modality_display': voice_caption.get_modality_display(),
                'display_duration': 'Text',  # Special display for text captions
                'quality_color': 'success',  # Text captions are always "good quality"
                'created_at': voice_caption.created_at.strftime('%b %d, %H:%M'),
                'audio_url': None,  # No audio for text captions
                'is_processed': True,  # Text is immediately processed
                'text_caption': voice_caption.text_caption,
                'is_text_caption': True  # Flag to identify text captions
            }
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def edit_voice_caption_transcription(request, patient_id, caption_id):
    """Edit the transcription of a voice caption"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    domain_models = get_domain_models(request)
    Patient = domain_models['Patient']
    VoiceCaption = domain_models['VoiceCaption']
    
    patient = get_object_or_404(Patient, patient_id=patient_id)
    voice_caption = get_object_or_404(VoiceCaption, id=caption_id, patient=patient)
    
    # Check permissions
    if not user_can_edit_caption(request.user, voice_caption):
        return JsonResponse({
            'error': 'You do not have permission to edit this transcription.',
            'code': 'permission_denied'
        }, status=403)
    
    try:
        data = json.loads(request.body)
        action = data.get('action')
        
        if action == 'edit':
            new_text = data.get('text', '').strip()
            if not new_text:
                return JsonResponse({'error': 'Transcription text cannot be empty'}, status=400)
            
            # Edit the transcription
            voice_caption.edit_transcription(new_text, request.user)
            
            return JsonResponse({
                'success': True,
                'message': 'Transcription updated successfully',
                'caption': {
                    'id': voice_caption.id,
                    'text_caption': voice_caption.text_caption,
                    'is_edited': voice_caption.is_edited,
                    'edit_history': voice_caption.edit_history
                }
            })
            
        elif action == 'revert':
            # Revert to original transcription
            voice_caption.revert_to_original(request.user)
            
            return JsonResponse({
                'success': True,
                'message': 'Transcription reverted to original',
                'caption': {
                    'id': voice_caption.id,
                    'text_caption': voice_caption.text_caption,
                    'is_edited': voice_caption.is_edited,
                    'edit_history': voice_caption.edit_history
                }
            })
            
        else:
            return JsonResponse({'error': 'Invalid action. Use "edit" or "revert"'}, status=400)
            
    except ValueError as e:
        return JsonResponse({'error': str(e)}, status=400)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_POST
def update_voice_caption_modality(request, patient_id, caption_id):
    """Update the modality of a voice caption"""
    domain_models = get_domain_models(request)
    Patient = domain_models['Patient']
    VoiceCaption = domain_models['VoiceCaption']

    patient = get_object_or_404(Patient, patient_id=patient_id)
    voice_caption = get_object_or_404(VoiceCaption, id=caption_id, patient=patient)

    # Check permissions
    if not user_can_edit_caption(request.user, voice_caption):
        return JsonResponse({
            'error': 'You do not have permission to edit this caption.',
            'code': 'permission_denied'
        }, status=403)
    
    try:
        data = json.loads(request.body)
        new_modality = data.get('modality', '').strip()
        
        if not new_modality:
            return JsonResponse({'error': 'Modality cannot be empty'}, status=400)
        
        # Validate modality against database
        from ..modality_helpers import is_valid_modality_slug
        if not is_valid_modality_slug(new_modality):
            return JsonResponse({'error': 'Invalid modality'}, status=400)
        
        # Update the modality
        voice_caption.modality = new_modality
        voice_caption.save()
        
        return JsonResponse({
            'success': True,
            'message': 'Modality updated successfully',
            'caption': {
                'id': voice_caption.id,
                'modality': voice_caption.modality,
                'modality_display': voice_caption.get_modality_display()
            }
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    except Exception as e:
        logger.error(f"Error updating caption modality: {e}", exc_info=True)
        return JsonResponse({'error': str(e)}, status=500)
