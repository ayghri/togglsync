"""URL configuration for the sync app."""

from django.urls import path

from . import views

app_name = 'sync'

urlpatterns = [
    # Landing page
    path('', views.home_page, name='landing'),

    # Health check
    path('health/', views.health_check, name='health_check'),

    # Legal pages (required for Google OAuth consent screen)
    path('privacy/', views.privacy_policy, name='privacy_policy'),
    path('terms/', views.terms_of_service, name='terms_of_service'),

    # Webhook endpoint with token for user identification
    path('webhook/toggl/<str:webhook_token>/', views.toggl_webhook, name='toggl_webhook'),

    # Google OAuth endpoints
    path('oauth/google/start/', views.google_oauth_start, name='google_oauth_start'),
    path('oauth/google/callback/', views.google_oauth_callback, name='google_callback'),
    path('oauth/google/disconnect/', views.google_oauth_disconnect, name='google_oauth_disconnect'),

    # Action endpoints
    path('actions/sync-toggl/', views.sync_toggl_metadata, name='sync_toggl_metadata'),
    path('actions/refresh-calendar/', views.refresh_calendar, name='refresh_calendar'),
]
