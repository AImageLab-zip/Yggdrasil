from django.urls import path, include
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from common.models import Project
from common.permissions import entry_project_for


@login_required
def set_laparoscopy(request):
    proj = entry_project_for(request.user, 'laparoscopy')
    if proj is None:
        proj = Project.objects.filter(slug='laparoscopy').first()
    if proj is None:
        proj = Project.objects.create(name='Laparoscopy', slug='laparoscopy', domain='laparoscopy')

    request.session['current_project_id'] = proj.id
    return redirect('laparoscopy:patient_list')


urlpatterns = [
    path('', set_laparoscopy, name='laparoscopy_home'),

    path('', include(('maxillo.app_urls', 'maxillo'), namespace='laparoscopy')),
]
