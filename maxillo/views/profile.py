"""User profile views."""
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db.models import Count, Q, Max
from django.utils import timezone
from datetime import timedelta

from ..models import Patient as MaxilloPatient, Classification as MaxilloClassification, VoiceCaption as MaxilloVoiceCaption
from .helpers import render_with_fallback, redirect_with_namespace
from common.models import ProjectAccess

import logging
logger = logging.getLogger(__name__)


def _get_domain_models(request):
    return MaxilloPatient, MaxilloClassification, MaxilloVoiceCaption


@login_required
def user_profile(request, username=None):
    """
    Display user profile with statistics and activity.
    
    Regular users can only view their own profile.
    Admins can view any user's profile.
    """
    # If no username provided, show current user's profile
    Patient, Classification, VoiceCaption = _get_domain_models(request)

    if username is None:
        target_user = request.user
    else:
        # Check if current user can view other profiles (admin or project manager)
        if not request.user.profile.can_view_other_profiles():
            messages.error(request, 'You do not have permission to view other user profiles.')
            return redirect_with_namespace(request, 'user_profile')
        
        # Get the target user
        target_user = get_object_or_404(User, username=username)
    
    # Resolve active project from URL-bound profile first (set by middleware),
    # fallback to session for safety.
    active_project_id = getattr(getattr(request.user, 'profile', None), 'project_id', None)
    if not active_project_id:
        active_project_id = request.session.get('current_project_id')

    has_project_field = any(field.name == 'project' for field in Patient._meta.fields)

    # Statistics (strictly scoped to active project)
    # 1. Patients uploaded
    patients_uploaded = Patient.objects.filter(
        uploaded_by=target_user
    ).order_by('-uploaded_at')
    if has_project_field:
        patients_uploaded = patients_uploaded.filter(project_id=active_project_id)
    total_patients_uploaded = patients_uploaded.count()
    
    # 2. Bite classifications (manual annotations)
    classifications = Classification.objects.filter(
        annotator=target_user,
        classifier='manual',
    ).select_related('patient').order_by('-timestamp')
    if has_project_field:
        classifications = classifications.filter(patient__project_id=active_project_id)
    total_classifications = classifications.count()
    
    # Get unique patients annotated (a patient might have been annotated multiple times)
    unique_patients_annotated = classifications.values('patient').distinct().count()
    
    # 3. Voice captions
    voice_captions = VoiceCaption.objects.filter(
        user=target_user
    ).select_related('patient').order_by('-created_at')
    if has_project_field:
        voice_captions = voice_captions.filter(patient__project_id=active_project_id)
    total_voice_captions = voice_captions.count()
    
    # Last activity timestamp
    last_activity = None
    last_activity_type = None
    
    # Check most recent activity across all types
    activities = []
    
    if patients_uploaded.exists():
        last_upload = patients_uploaded.first()
        activities.append(('upload', last_upload.uploaded_at))
    
    if classifications.exists():
        last_classification = classifications.first()
        activities.append(('classification', last_classification.timestamp))
    
    if voice_captions.exists():
        last_caption = voice_captions.first()
        activities.append(('voice_caption', last_caption.created_at))
    
    if activities:
        last_activity_type, last_activity = max(activities, key=lambda x: x[1])
    
    # Recent activity lists (last 20 of each)
    recent_uploads = patients_uploaded[:20]
    recent_classifications = classifications[:20]
    recent_voice_captions = voice_captions[:20]
    
    # Calculate activity in last 7 days
    seven_days_ago = timezone.now() - timedelta(days=7)
    
    uploads_last_7_days = Patient.objects.filter(
        uploaded_by=target_user,
        uploaded_at__gte=seven_days_ago
    ).count()
    if has_project_field:
        uploads_last_7_days = Patient.objects.filter(
            uploaded_by=target_user,
            project_id=active_project_id,
            uploaded_at__gte=seven_days_ago
        ).count()
    
    classifications_last_7_days = Classification.objects.filter(
        annotator=target_user,
        classifier='manual',
        timestamp__gte=seven_days_ago
    ).count()
    if has_project_field:
        classifications_last_7_days = Classification.objects.filter(
            annotator=target_user,
            classifier='manual',
            patient__project_id=active_project_id,
            timestamp__gte=seven_days_ago
        ).count()
    
    voice_captions_last_7_days = VoiceCaption.objects.filter(
        user=target_user,
        created_at__gte=seven_days_ago
    ).count()
    if has_project_field:
        voice_captions_last_7_days = VoiceCaption.objects.filter(
            user=target_user,
            patient__project_id=active_project_id,
            created_at__gte=seven_days_ago
        ).count()

    # Get target user's ProjectAccess for current project
    target_profile = None
    if active_project_id:
        try:
            target_profile = ProjectAccess.objects.get(
                user=target_user,
                project_id=active_project_id
            )
        except ProjectAccess.DoesNotExist:
            target_profile = None

    context = {
        'target_user': target_user,
        'target_profile': target_profile,
        'is_own_profile': target_user == request.user,
        'is_viewing_other_profile': request.user.profile.can_view_other_profiles() and target_user != request.user,
        
        # Statistics
        'total_patients_uploaded': total_patients_uploaded,
        'total_classifications': total_classifications,
        'unique_patients_annotated': unique_patients_annotated,
        'total_voice_captions': total_voice_captions,
        
        # Last activity
        'last_activity': last_activity,
        'last_activity_type': last_activity_type,
        
        # Recent activity
        'recent_uploads': recent_uploads,
        'recent_classifications': recent_classifications,
        'recent_voice_captions': recent_voice_captions,
        
        # Last 7 days stats
        'uploads_last_7_days': uploads_last_7_days,
        'classifications_last_7_days': classifications_last_7_days,
        'voice_captions_last_7_days': voice_captions_last_7_days,
    }
    
    # Add all users list for dropdown (only on own profile for admins/project managers)
    if request.user.profile.can_view_other_profiles() and target_user == request.user:
        all_users = User.objects.order_by('username')
        context['all_users'] = all_users
    
    return render_with_fallback(request, 'user_profile', context)
