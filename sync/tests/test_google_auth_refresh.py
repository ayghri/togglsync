"""Tests for Google OAuth token refresh."""

import json
from datetime import timedelta
from unittest.mock import Mock, patch, MagicMock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from sync.services.gcal import GoogleCalendarService


class GoogleAuthRefreshTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("testuser", password="pass")
        expiry = timezone.now() - timedelta(minutes=5)
        self.creds_data = {
            "token": "mock_token",
            "refresh_token": "mock_refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "cid", "client_secret": "csec",
            "scopes": ["https://www.googleapis.com/auth/calendar.events"],
            "expiry": expiry.isoformat(),
        }
        self.user.credentials.gauth_credentials_json = json.dumps(self.creds_data)
        self.user.credentials.save()

    def _mock_creds(self, expired=True):
        m = Mock()
        m.expired = expired
        m.refresh = Mock()
        m.to_json.return_value = json.dumps(self.creds_data)
        return m

    def test_detects_expired(self):
        with patch('sync.services.gcal.build'), \
             patch('sync.services.gcal.Credentials') as cls:
            cls.from_authorized_user_info.return_value = self._mock_creds(True)
            svc = GoogleCalendarService(user=self.user)
            self.assertTrue(svc.credentials.expired)

    def test_refresh_called_when_expired(self):
        with patch('sync.services.gcal.build'), \
             patch('sync.services.gcal.Credentials') as cls, \
             patch('sync.services.gcal.Request'):
            mock_creds = self._mock_creds(True)
            cls.from_authorized_user_info.return_value = mock_creds
            svc = GoogleCalendarService(user=self.user)
            mock_creds.refresh.reset_mock()
            svc._refresh_maybe()
            mock_creds.refresh.assert_called_once()

    def test_refreshed_token_saved(self):
        new_data = {**self.creds_data, "token": "new_tok"}
        with patch('sync.services.gcal.build'), \
             patch('sync.services.gcal.Credentials') as cls, \
             patch('sync.services.gcal.Request'):
            mock_creds = self._mock_creds(True)
            mock_creds.to_json.return_value = json.dumps(new_data)
            cls.from_authorized_user_info.return_value = mock_creds
            GoogleCalendarService(user=self.user)
            self.user.credentials.refresh_from_db()
            saved = json.loads(self.user.credentials.gauth_credentials_json)
            self.assertEqual(saved["token"], "new_tok")

    def test_no_refresh_when_valid(self):
        with patch('sync.services.gcal.build'), \
             patch('sync.services.gcal.Credentials') as cls:
            mock_creds = self._mock_creds(False)
            cls.from_authorized_user_info.return_value = mock_creds
            svc = GoogleCalendarService(user=self.user)
            svc._refresh_maybe()
            mock_creds.refresh.assert_not_called()

    def test_refresh_updates_timestamp(self):
        orig = self.user.credentials.updated_at
        with patch('sync.services.gcal.build'), \
             patch('sync.services.gcal.Credentials') as cls, \
             patch('sync.services.gcal.Request'):
            cls.from_authorized_user_info.return_value = self._mock_creds(True)
            GoogleCalendarService(user=self.user)
            self.user.credentials.refresh_from_db()
            self.assertGreater(self.user.credentials.updated_at, orig)

    def test_refresh_called_before_api(self):
        with patch('sync.services.gcal.build') as build, \
             patch('sync.services.gcal.Credentials') as cls:
            mock_creds = self._mock_creds(False)
            cls.from_authorized_user_info.return_value = mock_creds
            build.return_value = MagicMock()
            svc = GoogleCalendarService(user=self.user)
            with patch.object(svc, '_refresh_maybe', wraps=svc._refresh_maybe) as spy:
                try:
                    svc.find_event_by_ical_uid("cal", "uid")
                except:
                    pass
                spy.assert_called()
