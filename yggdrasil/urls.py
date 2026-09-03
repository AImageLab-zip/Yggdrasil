"""
URL configuration for yggdrasil project.

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
    path(
        "admin/online-users/",
        common_views.online_users_dashboard,
        name="online_users_dashboard",
    ),
    path(
        "admin/online-users/api/",
        common_views.online_users_api,
        name="online_users_api",
    ),
    path(
        "admin/user-activity/",
        common_views.user_activity_stats,
        name="user_activity_stats",
    ),
    path("admin/", admin.site.urls),
    path("status/", common_views.status_page, name="status_page"),
    path("maintenance/", common_views.maintenance_page, name="maintenance_page"),
    path("changelog/", common_views.changelog_page, name="changelog_page"),
    path("healthz", common_views.healthz, name="healthz"),
    path(
        "api/preferences/report-language/",
        common_views.set_report_language,
        name="set_report_language",
    ),
    path("api/notifications/", common_views.notifications_api, name="notifications_api"),
    path(
        "api/notifications/mark-read/",
        common_views.notifications_mark_read,
        name="notifications_mark_read",
    ),
    path("", scans_views.home, name="home"),
    path("maxillo/", include("maxillo.urls")),
    path("brain/", include("brain.urls")),
    path("laparoscopy/", include("laparoscopy.urls")),
    # Public anonymous read-only demo (Phase 7)
    path("demo/", include("common.demo_urls")),
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
