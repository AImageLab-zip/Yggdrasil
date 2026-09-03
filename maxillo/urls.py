from django.urls import path, include
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from common.models import Project
from common.permissions import entry_project_for


@login_required
def set_maxillo(request):
    # Enter the domain at the user's first accessible project (fallback: the
    # catch-all / first active project of the domain).
    proj = entry_project_for(request.user, 'maxillo')
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
