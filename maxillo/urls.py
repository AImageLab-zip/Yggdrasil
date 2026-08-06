from django.urls import path, include
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from common.models import Project, ProjectAccess


@login_required
def set_maxillo(request):
    # Enter the domain at the user's first accessible project (fallback: the
    # catch-all / first active project of the domain).
    accessible = ProjectAccess.objects.filter(user=request.user).values_list('project_id', flat=True)
    proj = (
        Project.objects.filter(domain='maxillo', is_active=True)
        .filter(id__in=accessible)
        .order_by('name')
        .first()
        or Project.objects.filter(domain='maxillo', is_active=True).order_by('name').first()
    )
    if proj is None:
        proj = Project.objects.filter(slug='maxillo').first()
    if proj is None:
        proj = Project.objects.create(name='Maxillo', slug='maxillo', domain='maxillo')

    request.session['current_project_id'] = proj.id
    return redirect('maxillo:patient_list')


urlpatterns = [
    path('', set_maxillo, name='maxillo_home'),
    path('', include(('maxillo.app_urls', 'maxillo'), namespace='maxillo')),
]
