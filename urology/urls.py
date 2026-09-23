from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.urls import include, path

from common.models import Project
from common.permissions import entry_project_for


@login_required
def set_urology(request):
    proj = entry_project_for(request.user, "urology")
    if proj is None:
        proj = Project.objects.filter(slug="urology").first()
    if proj is None:
        proj = Project.objects.create(
            name="Urology",
            slug="urology",
            domain="urology",
            icon="fas fa-microscope",
        )

    request.session["current_project_id"] = proj.id
    return redirect("urology:patient_list")


urlpatterns = [
    path("", set_urology, name="urology_home"),
    path("", include(("urology.app_urls", "urology"), namespace="urology")),
]
