"""Tests for Toggl webhook view."""

import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, RequestFactory

from sync.models import TogglWorkspace, TogglTimeEntry
from sync.views import toggl_webhook


class TogglWebhookTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user("testuser", password="pass")
        self.ws = TogglWorkspace.objects.create(
            user=self.user, toggl_id=1, name="WS", webhook_token="tok_abc",
        )

    def _post(self, token, payload):
        request = self.factory.post(
            f"/webhook/toggl/{token}/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        return toggl_webhook(request, webhook_token=token)

    def test_unknown_token_returns_404(self):
        self.assertEqual(self._post("bad", {"payload": "ping"}).status_code, 404)

    def test_ping_returns_validation_code(self):
        resp = self._post("tok_abc", {"payload": "ping", "validation_code": "abc"})
        self.assertEqual(json.loads(resp.content)["validation_code"], "abc")

    def test_invalid_json_returns_400(self):
        request = self.factory.post("/", data="bad", content_type="application/json")
        self.assertEqual(toggl_webhook(request, webhook_token="tok_abc").status_code, 400)

    @patch("sync.views.async_task")
    def test_created_saves_entry_and_queues_task(self, mock_async):
        self._post("tok_abc", {
            "payload": {"id": 123, "description": "Work", "start": "2026-02-27T10:00:00Z",
                        "stop": "2026-02-27T11:00:00Z", "project_id": None, "tag_ids": []},
            "metadata": {"action": "created"},
        })
        entry = TogglTimeEntry.objects.get(user=self.user, toggl_id=123)
        self.assertEqual(entry.description, "Work")
        self.assertFalse(entry.synced)
        mock_async.assert_called_once()
        self.assertEqual(mock_async.call_args[0][:3],
                         ("sync.tasks.process_time_entry_event", self.user.id, 123))

    @patch("sync.views.async_task")
    def test_updated_resets_synced(self, mock_async):
        TogglTimeEntry.objects.create(
            user=self.user, toggl_id=123, description="Old",
            start_time="2026-02-27T10:00:00Z", end_time="2026-02-27T11:00:00Z", synced=True,
        )
        self._post("tok_abc", {
            "payload": {"id": 123, "description": "New", "start": "2026-02-27T10:00:00Z",
                        "stop": "2026-02-27T11:30:00Z", "project_id": None, "tag_ids": []},
            "metadata": {"action": "updated"},
        })
        entry = TogglTimeEntry.objects.get(user=self.user, toggl_id=123)
        self.assertEqual(entry.description, "New")
        self.assertFalse(entry.synced)

    @patch("sync.views.async_task")
    def test_deleted_sets_pending_deletion(self, mock_async):
        TogglTimeEntry.objects.create(
            user=self.user, toggl_id=123, description="Del",
            start_time="2026-02-27T10:00:00Z", end_time="2026-02-27T11:00:00Z", synced=True,
        )
        self._post("tok_abc", {"payload": {"id": 123}, "metadata": {"action": "deleted"}})
        entry = TogglTimeEntry.objects.get(user=self.user, toggl_id=123)
        self.assertTrue(entry.pending_deletion)
        self.assertFalse(entry.synced)

    @patch("sync.views.async_task")
    def test_unknown_action_ignored(self, mock_async):
        self._post("tok_abc", {"payload": {"id": 99}, "metadata": {"action": "unknown"}})
        mock_async.assert_not_called()

    @patch("sync.views.async_task")
    def test_missing_id_ignored(self, mock_async):
        self._post("tok_abc", {"payload": {"description": "no id"}, "metadata": {"action": "created"}})
        mock_async.assert_not_called()
