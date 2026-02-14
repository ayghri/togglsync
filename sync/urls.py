"""URL configuration for the sync app."""

from django.urls import path

from . import views

app_name = 'sync'

urlpatterns = [
    # Landing page
    path('', views.home_page, name='landing'),

    # Health check
    path('health/', views.health_check, name='health_check'),

    # Webhook endpoint with token for user identification
    path('webhook/toggl/<str:webhook_token>/', views.toggl_webhook, name='toggl_webhook'),

    # Google OAuth endpoints
    path('oauth/google/start/', views.google_oauth_start, name='google_oauth_start'),
    path('oauth/google/callback/', views.google_oauth_callback, name='google_callback'),
    path('oauth/google/disconnect/', views.google_oauth_disconnect, name='google_oauth_disconnect'),

    # Action endpoints
    path('actions/import-calendars/', views.import_calendars, name='import_calendars'),
    path('actions/sync-toggl/', views.sync_toggl_metadata, name='sync_toggl_metadata'),
]
