from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.urls import include, path

from common.models import Project
from common.permissions import entry_project_for


@login_required
def set_cardiology(request):
    proj = entry_project_for(request.user, "cardiology")
    if proj is None:
        proj = Project.objects.filter(slug="cardiology").first()
    if proj is None:
        proj = Project.objects.create(
            name="Cardiology", slug="cardiology", domain="cardiology", icon="fas fa-heart-pulse"
        )

    request.session["current_project_id"] = proj.id
    return redirect("cardiology:patient_list")


urlpatterns = [
    path("", set_cardiology, name="cardiology_home"),
    path("", include(("cardiology.app_urls", "cardiology"), namespace="cardiology")),
]
