from django.urls import path, include
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages

from common.models import Project, ProjectAccess


@login_required
def set_brain(request):
    accessible = ProjectAccess.objects.filter(user=request.user).values_list('project_id', flat=True)
    proj = (
        Project.objects.filter(domain='brain', is_active=True)
        .filter(id__in=accessible)
        .order_by('name')
        .first()
        or Project.objects.filter(domain='brain', is_active=True).order_by('name').first()
    )
    if proj is None:
        proj = Project.objects.filter(slug='brain').first()
    if proj is None:
        proj = Project.objects.create(name='Brain', slug='brain', domain='brain')

    request.session['current_project_id'] = proj.id
    return redirect('brain:patient_list')


# NOTE: report-language persistence moved to the cross-app endpoint
# `set_report_language` in common.views (URL name unchanged), so all three
# domains share one preference (common.UserPreference). See toothfairy/urls.py.


urlpatterns = [
    path('', set_brain, name='brain_home'),
    path('', include(('brain.app_urls', 'brain'), namespace='brain')),
]
