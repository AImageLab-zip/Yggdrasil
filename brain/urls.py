from django.urls import path, include
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
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


urlpatterns = [
    path('', set_brain, name='brain_home'),
    path('', include(('maxillo.app_urls', 'maxillo'), namespace='brain')),
]
