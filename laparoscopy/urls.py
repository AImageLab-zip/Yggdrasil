from django.urls import path, include
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from common.models import Project, ProjectAccess
from laparoscopy import views as laparo_views


@login_required
def set_laparoscopy(request):
    accessible = ProjectAccess.objects.filter(user=request.user).values_list('project_id', flat=True)
    proj = (
        Project.objects.filter(domain='laparoscopy', is_active=True)
        .filter(id__in=accessible)
        .order_by('name')
        .first()
        or Project.objects.filter(domain='laparoscopy', is_active=True).order_by('name').first()
    )
    if proj is None:
        proj = Project.objects.filter(slug='laparoscopy').first()
    if proj is None:
        proj = Project.objects.create(name='Laparoscopy', slug='laparoscopy', domain='laparoscopy')

    request.session['current_project_id'] = proj.id
    return redirect('laparoscopy:patient_list')


urlpatterns = [
    path('', set_laparoscopy, name='laparoscopy_home'),

    # Quadrant classification API
    path('api/patient/<int:patient_id>/quadrant-markers/', laparo_views.patient_quadrant_markers, name='patient_quadrant_markers'),
    path('api/quadrant-types/', laparo_views.quadrant_types, name='quadrant_types'),
    path('api/quadrant-types/<int:pk>/', laparo_views.quadrant_type_detail, name='quadrant_type_detail'),

    # Region annotation API. One whole-state route, GET and PUT: the per-stroke
    # create/patch/delete endpoints went with the strokes themselves in Phase 10 --
    # decision #14 makes the labelmap canonical, and there is no "the stroke with id 41"
    # to address once the eraser has mutated the pixels it drew.
    path('api/patient/<int:patient_id>/video-annotations/', laparo_views.patient_video_annotations, name='patient_video_annotations'),
    path('api/region-types/', laparo_views.region_types, name='region_types'),
    path('api/region-types/<int:pk>/', laparo_views.region_type_detail, name='region_type_detail'),

    # Magic Tool worker proxy API
    path('api/worker/session-ready/', laparo_views.worker_session_ready, name='worker_session_ready'),
    path('api/worker/session-prompt/', laparo_views.worker_session_prompt, name='worker_session_prompt'),

    path('', include(('maxillo.app_urls', 'maxillo'), namespace='laparoscopy')),
]
