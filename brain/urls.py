import json

from django.urls import path, include
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from common.models import Project, ProjectAccess


@login_required
def set_brain(request):
    proj = Project.objects.filter(slug='brain').first() or Project.objects.filter(name__iexact='brain').first()
    if not proj:
        proj = Project.objects.create(name='brain', slug='brain')

    # Check if user has access to Brain project
    if not request.user.profile.is_admin():
        has_access = ProjectAccess.objects.filter(
            user=request.user,
            project=proj
        ).exists()
        if not has_access:
            messages.error(request, "You don't have access to the Brain project.")
            return redirect('home')

    request.session['current_project_id'] = proj.id
    return redirect('brain:patient_list')


@login_required
@require_POST
def set_report_language(request):
    """AJAX endpoint: save the user's preferred report language (brain only)."""
    from brain.models import UserPreference
    try:
        body = json.loads(request.body)
        language = body.get('language', 'it')
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid request'}, status=400)

    if language not in ('it', 'en'):
        return JsonResponse({'error': 'Invalid language'}, status=400)

    pref, _ = UserPreference.objects.get_or_create(user=request.user)
    pref.report_language = language
    pref.save(update_fields=['report_language', 'updated_at'])
    return JsonResponse({'ok': True, 'language': language})


urlpatterns = [
    path('', set_brain, name='brain_home'),
    path('api/preferences/report-language/', set_report_language, name='set_report_language'),
    path('', include(('brain.app_urls', 'brain'), namespace='brain')),
]
