from django.urls import path, include
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from common.models import Project, ProjectAccess


@login_required
def set_maxillo(request):
    proj = Project.objects.filter(slug='maxillo').first() or Project.objects.filter(name__iexact='maxillo').first()
    if not proj:
        proj = Project.objects.create(name='maxillo', slug='maxillo')

    # Check if user has access to Maxillo project
    if not request.user.profile.is_admin():
        has_access = ProjectAccess.objects.filter(
            user=request.user,
            project=proj
        ).exists()
        if not has_access:
            messages.error(request, "You don't have access to the Maxillo project.")
            return redirect('home')

    request.session['current_project_id'] = proj.id
    return redirect('maxillo:patient_list')


urlpatterns = [
    path('', set_maxillo, name='maxillo_home'),
    path('', include(('maxillo.app_urls', 'maxillo'), namespace='maxillo')),
]
