"""
URL configuration for toothfairy project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from maxillo import views as scans_views
from common import views as common_views

urlpatterns = [
    # App-agnostic admin control panel (must come before Django admin route)
    path(
        "admin/control-panel/",
        common_views.admin_control_panel,
        name="admin_control_panel",
    ),
    path("admin/", admin.site.urls),
    path("", scans_views.home, name="home"),
    path("maxillo/", include("maxillo.urls")),
    path("brain/", include("brain.urls")),
    # API root
    path("api/", include(("maxillo.api_urls", "api"), namespace="api")),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path(
        "logout/",
        auth_views.LogoutView.as_view(template_name="registration/logged_out.html"),
        name="logout",
    ),
    path("register/", scans_views.register, name="register"),
    path("invitations/", scans_views.invitation_list, name="invitation_list"),
    path(
        "invitations/<str:code>/delete/",
        scans_views.delete_invitation,
        name="delete_invitation",
    ),
]
