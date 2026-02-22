"""Tests for Google OAuth token refresh functionality."""

import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from sync.models import UserCredentials
from sync.services.gcal import GoogleCalendarService, GoogleCalendarError


class GoogleAuthRefreshTestCase(TestCase):
    """Test automatic token refresh for Google OAuth."""

    def setUp(self):
        """Create test user and credentials."""
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )

        # Create mock credentials that will expire soon
        expiry = timezone.now() - timedelta(minutes=5)  # Already expired

        self.mock_creds_data = {
            "token": "mock_access_token_123",
            "refresh_token": "mock_refresh_token_456",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "mock_client_id",
            "client_secret": "mock_client_secret",
            "scopes": ["https://www.googleapis.com/auth/calendar.events"],
            "expiry": expiry.isoformat(),
        }

        self.user.credentials.gauth_credentials_json = json.dumps(self.mock_creds_data)
        self.user.credentials.save()

    def test_credentials_are_expired(self):
        """Test that we can detect expired credentials."""
        with patch('sync.services.gcal.build'), \
             patch('sync.services.gcal.Credentials') as mock_creds_class:

            # Mock credentials object
            mock_creds = Mock()
            mock_creds.expired = True
            mock_creds.to_json.return_value = json.dumps(self.mock_creds_data)
            mock_creds_class.from_authorized_user_info.return_value = mock_creds

            service = GoogleCalendarService(user=self.user)

            # Verify credentials are detected as expired
            self.assertTrue(service.credentials.expired)

    def test_refresh_is_called_when_expired(self):
        """Test that refresh() is called when credentials are expired."""
        with patch('sync.services.gcal.build'), \
             patch('sync.services.gcal.Credentials') as mock_creds_class, \
             patch('sync.services.gcal.Request') as mock_request:

            # Mock credentials object
            mock_creds = Mock()
            mock_creds.expired = True
            mock_creds.refresh = Mock()
            mock_creds.to_json.return_value = json.dumps({
                **self.mock_creds_data,
                "token": "new_access_token_789",
                "expiry": (timezone.now() + timedelta(hours=1)).isoformat(),
            })
            mock_creds_class.from_authorized_user_info.return_value = mock_creds

            service = GoogleCalendarService(user=self.user)
            # Reset mock to ignore the refresh call from __init__
            mock_creds.refresh.reset_mock()

            service._refresh_maybe()

            # Verify refresh was called
            mock_creds.refresh.assert_called_once()

    def test_refreshed_token_is_saved_to_database(self):
        """Test that refreshed token is persisted to the database."""
        new_token = "new_refreshed_token_xyz"
        new_expiry = timezone.now() + timedelta(hours=1)

        with patch('sync.services.gcal.build'), \
             patch('sync.services.gcal.Credentials') as mock_creds_class, \
             patch('sync.services.gcal.Request'):

            # Mock credentials object
            mock_creds = Mock()
            mock_creds.expired = True
            mock_creds.refresh = Mock()

            new_creds_data = {
                **self.mock_creds_data,
                "token": new_token,
                "expiry": new_expiry.isoformat(),
            }
            mock_creds.to_json.return_value = json.dumps(new_creds_data)
            mock_creds_class.from_authorized_user_info.return_value = mock_creds

            service = GoogleCalendarService(user=self.user)
            service._refresh_maybe()

            # Reload credentials from database
            self.user.credentials.refresh_from_db()
            saved_creds = json.loads(self.user.credentials.gauth_credentials_json)

            # Verify new token was saved
            self.assertEqual(saved_creds["token"], new_token)

    def test_refresh_before_api_calls(self):
        """Test that _refresh_maybe is called before API operations."""
        with patch('sync.services.gcal.build') as mock_build, \
             patch('sync.services.gcal.Credentials') as mock_creds_class:

            # Mock credentials object
            mock_creds = Mock()
            mock_creds.expired = False
            mock_creds.to_json.return_value = json.dumps(self.mock_creds_data)
            mock_creds_class.from_authorized_user_info.return_value = mock_creds

            # Mock service
            mock_service = MagicMock()
            mock_build.return_value = mock_service

            service = GoogleCalendarService(user=self.user)

            # Spy on _refresh_maybe
            with patch.object(service, '_refresh_maybe', wraps=service._refresh_maybe) as spy:
                # Call an API method
                try:
                    service.get_calendar("test_calendar_id")
                except:
                    pass  # We don't care if it fails, just that refresh was called

                # Verify refresh check was called
                spy.assert_called()

    def test_no_refresh_when_not_expired(self):
        """Test that refresh is NOT called when credentials are still valid."""
        # Create non-expired credentials
        future_expiry = timezone.now() + timedelta(hours=1)

        valid_creds_data = {
            **self.mock_creds_data,
            "expiry": future_expiry.isoformat(),
        }

        self.user.credentials.gauth_credentials_json = json.dumps(valid_creds_data)
        self.user.credentials.save()

        with patch('sync.services.gcal.build'), \
             patch('sync.services.gcal.Credentials') as mock_creds_class:

            # Mock credentials object
            mock_creds = Mock()
            mock_creds.expired = False
            mock_creds.refresh = Mock()
            mock_creds.to_json.return_value = json.dumps(valid_creds_data)
            mock_creds_class.from_authorized_user_info.return_value = mock_creds

            service = GoogleCalendarService(user=self.user)
            service._refresh_maybe()

            # Verify refresh was NOT called
            mock_creds.refresh.assert_not_called()

    def test_refresh_updates_updated_at_timestamp(self):
        """Test that database updated_at timestamp is updated on refresh."""
        with patch('sync.services.gcal.build'), \
             patch('sync.services.gcal.Credentials') as mock_creds_class, \
             patch('sync.services.gcal.Request'):

            # Record original timestamp
            original_updated_at = self.user.credentials.updated_at

            # Mock credentials object
            mock_creds = Mock()
            mock_creds.expired = True
            mock_creds.refresh = Mock()
            mock_creds.to_json.return_value = json.dumps(self.mock_creds_data)
            mock_creds_class.from_authorized_user_info.return_value = mock_creds

            service = GoogleCalendarService(user=self.user)
            service._refresh_maybe()

            # Reload and check timestamp changed
            self.user.credentials.refresh_from_db()
            self.assertGreater(
                self.user.credentials.updated_at,
                original_updated_at
            )
