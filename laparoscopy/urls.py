from django.urls import path, include
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from common.models import Project, ProjectAccess
from laparoscopy import views as laparo_views


@login_required
def set_laparoscopy(request):
    proj = Project.objects.filter(slug='laparoscopy').first()
    if not proj:
        proj = Project.objects.create(name='laparoscopy', slug='laparoscopy')

    if not (request.user.profile.is_admin or request.user.profile.is_student_developer()):
        has_access = ProjectAccess.objects.filter(user=request.user, project=proj).exists()
        if not has_access:
            messages.error(request, "You don't have access to the laparoscopy project.")
            return redirect('home')

    request.session['current_project_id'] = proj.id
    return redirect('laparoscopy:patient_list')


urlpatterns = [
    path('', set_laparoscopy, name='laparoscopy_home'),

    # Quadrant classification API
    path('api/patient/<int:patient_id>/quadrant-markers/', laparo_views.patient_quadrant_markers, name='patient_quadrant_markers'),
    path('api/quadrant-types/', laparo_views.quadrant_types, name='quadrant_types'),
    path('api/quadrant-types/<int:pk>/', laparo_views.quadrant_type_detail, name='quadrant_type_detail'),

    # Region annotation API
    path('api/patient/<int:patient_id>/annotations/', laparo_views.patient_region_annotations, name='patient_region_annotations'),
    path('api/annotations/<int:annotation_id>/', laparo_views.region_annotation_detail, name='region_annotation_detail'),
    path('api/region-types/', laparo_views.region_types, name='region_types'),
    path('api/region-types/<int:pk>/', laparo_views.region_type_detail, name='region_type_detail'),

    # Magic Tool worker proxy API
    path('api/worker/session-ready/', laparo_views.worker_session_ready, name='worker_session_ready'),
    path('api/worker/session-prompt/', laparo_views.worker_session_prompt, name='worker_session_prompt'),

    path('', include(('maxillo.app_urls', 'maxillo'), namespace='laparoscopy')),
]
