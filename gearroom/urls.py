"""
URL configuration for gearroom project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
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
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path

from inventory.admin_views import (
    master_log_view,
    site_settings_view,
    user_create_view,
    user_delete_view,
    user_edit_view,
    user_toggle_active_view,
    users_list_view,
)

urlpatterns = [
    path('inventory/admin/settings-view/', site_settings_view, name='site_settings'),
    path('inventory/admin/master-log/', master_log_view, name='admin_master_log'),
    path('inventory/admin/users/', users_list_view, name='admin_users'),
    path('inventory/admin/users/new/', user_create_view, name='admin_user_create'),
    path('inventory/admin/users/<int:pk>/edit/', user_edit_view, name='admin_user_edit'),
    path('inventory/admin/users/<int:pk>/delete/', user_delete_view, name='admin_user_delete'),
    path('inventory/admin/users/<int:pk>/toggle-active/', user_toggle_active_view, name='admin_user_toggle_active'),
    path('inventory/admin/', admin.site.urls),
    path('inventory/accounts/', include('allauth.urls')),
    path('inventory/', include('inventory.urls')),
    # Redirect the bare prefix and root to the app home.
    path('', lambda request: redirect('inventory:home')),
    path('inventory', lambda request: redirect('inventory:home')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
