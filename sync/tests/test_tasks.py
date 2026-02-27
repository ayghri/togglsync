"""Tests for sync tasks, models, and color resolution."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from sync.models import (
    TogglTimeEntry, TogglWorkspace, TogglProject,
    TogglTag, TogglOrganization, EntityColorMapping,
)
from sync.services.gcal import GoogleCalendarError
from sync.services.toggl import TogglAPIError
from sync.tasks import (
    process_time_entry_event, _sync_to_calendar, _handle_deleted,
    _refresh_unknown_metadata, apply_color_to_entry, validate_synced_events,
)

GCAL = patch("sync.tasks.GoogleCalendarService")


def _make_gcal(mock_cls, find_return=None):
    m = MagicMock()
    mock_cls.return_value = m
    m.ensure_toggl_calendar.return_value = "cal_id"
    m.find_event_by_ical_uid.return_value = find_return
    m.create_event.return_value = {"id": "evt1"}
    return m


class ProcessTimeEntryEventTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("testuser", password="pass")
        self.user.credentials.gauth_credentials_json = '{"token": "t"}'
        self.user.credentials.save()
        self.entry = TogglTimeEntry.objects.create(
            user=self.user, toggl_id=100, description="Test",
            start_time=timezone.now() - timedelta(hours=1),
            end_time=timezone.now(), synced=False,
        )

    def test_skips_missing_user_and_entry(self):
        process_time_entry_event(9999, 100)
        process_time_entry_event(self.user.id, 9999)

    def test_skips_if_not_connected(self):
        self.user.credentials.gauth_credentials_json = ""
        self.user.credentials.save()
        process_time_entry_event(self.user.id, 100)
        self.entry.refresh_from_db()
        self.assertFalse(self.entry.synced)

    @GCAL
    def test_syncs_and_marks_synced(self, mock_cls):
        _make_gcal(mock_cls)
        process_time_entry_event(self.user.id, 100)
        self.entry.refresh_from_db()
        self.assertTrue(self.entry.synced)

    @GCAL
    def test_handles_deletion(self, mock_cls):
        self.entry.pending_deletion = True
        self.entry.save()
        gcal = _make_gcal(mock_cls, {"id": "evt1"})
        process_time_entry_event(self.user.id, 100)
        gcal.delete_event.assert_called_once_with("cal_id", "evt1")
        self.entry.refresh_from_db()
        self.assertTrue(self.entry.synced)

    @GCAL
    def test_exception_does_not_mark_synced(self, mock_cls):
        mock_cls.side_effect = GoogleCalendarError("fail")
        process_time_entry_event(self.user.id, 100)
        self.entry.refresh_from_db()
        self.assertFalse(self.entry.synced)

    @GCAL
    def test_concurrent_update_prevents_synced_flag(self, mock_cls):
        """If entry is modified mid-sync, synced stays False."""
        gcal = _make_gcal(mock_cls)

        def concurrent_update(**kwargs):
            fresh = TogglTimeEntry.objects.get(id=self.entry.id)
            fresh.description = "Updated by webhook"
            fresh.synced = False
            fresh.save()
            return {"id": "evt1"}

        gcal.create_event.side_effect = concurrent_update
        process_time_entry_event(self.user.id, 100)
        self.entry.refresh_from_db()
        self.assertFalse(self.entry.synced)
        self.assertEqual(self.entry.description, "Updated by webhook")


class SyncToCalendarTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("testuser", password="pass")
        self.user.credentials.gauth_credentials_json = '{"token": "t"}'
        self.user.credentials.save()
        self.entry = TogglTimeEntry.objects.create(
            user=self.user, toggl_id=200, description="My task",
            start_time=timezone.now() - timedelta(hours=1),
            end_time=timezone.now(), synced=False,
        )

    @GCAL
    def test_creates_when_not_existing(self, mock_cls):
        gcal = _make_gcal(mock_cls)
        _sync_to_calendar(self.entry)
        gcal.create_event.assert_called_once()

    @GCAL
    def test_updates_when_existing(self, mock_cls):
        gcal = _make_gcal(mock_cls, {"id": "existing"})
        _sync_to_calendar(self.entry)
        gcal.update_event.assert_called_once()
        self.assertEqual(gcal.update_event.call_args.kwargs["event_id"], "existing")
        self.assertEqual(gcal.update_event.call_args.kwargs["summary"], "My task")
        gcal.create_event.assert_not_called()

    @GCAL
    def test_uses_color_mapping(self, mock_cls):
        ws = TogglWorkspace.objects.create(user=self.user, toggl_id=1, name="WS")
        TogglProject.objects.create(user=self.user, toggl_id=10, workspace=ws, name="P")
        EntityColorMapping.objects.create(
            user=self.user, entity_type="project", entity_id=10,
            entity_name="P", color_name="Tomato", process_order=1,
        )
        self.entry.project_id = 10
        self.entry.save()
        gcal = _make_gcal(mock_cls)
        _sync_to_calendar(self.entry)
        self.assertEqual(gcal.create_event.call_args.kwargs["color_id"], "11")

    @GCAL
    def test_running_entry_gets_grey(self, mock_cls):
        self.entry.end_time = None
        self.entry.save()
        gcal = _make_gcal(mock_cls)
        _sync_to_calendar(self.entry)
        self.assertEqual(gcal.create_event.call_args.kwargs["color_id"], "8")

    @GCAL
    def test_no_description_placeholder(self, mock_cls):
        self.entry.description = ""
        self.entry.save()
        gcal = _make_gcal(mock_cls)
        _sync_to_calendar(self.entry)
        self.assertEqual(gcal.create_event.call_args.kwargs["summary"], "(No description)")


class HandleDeletedTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("testuser", password="pass")
        self.user.credentials.gauth_credentials_json = '{"token": "t"}'
        self.user.credentials.save()
        self.entry = TogglTimeEntry.objects.create(
            user=self.user, toggl_id=300, description="Del",
            start_time=timezone.now() - timedelta(hours=1),
            end_time=timezone.now(), pending_deletion=True,
        )

    @GCAL
    def test_deletes_existing(self, mock_cls):
        gcal = _make_gcal(mock_cls, {"id": "evt"})
        _handle_deleted(self.entry)
        gcal.delete_event.assert_called_once_with("cal_id", "evt")

    @GCAL
    def test_noop_when_already_gone(self, mock_cls):
        gcal = _make_gcal(mock_cls)
        _handle_deleted(self.entry)
        gcal.delete_event.assert_not_called()


class RefreshUnknownMetadataTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("testuser", password="pass")
        self.user.credentials.toggl_api_token = "tok"
        self.user.credentials.save()
        self.ws = TogglWorkspace.objects.create(user=self.user, toggl_id=1, name="WS")
        self.entry = TogglTimeEntry.objects.create(
            user=self.user, toggl_id=400, description="X",
            start_time=timezone.now(), project_id=999,
        )

    @patch("sync.tasks.TogglService")
    def test_fetches_unknown_project(self, mock_cls):
        mock_cls.return_value.get_projects.return_value = [
            {"id": 999, "name": "New", "color": "#f00", "active": True}
        ]
        mock_cls.return_value.get_tags.return_value = []
        _refresh_unknown_metadata(self.entry)
        self.assertTrue(TogglProject.objects.filter(user=self.user, toggl_id=999).exists())

    @patch("sync.tasks.TogglService")
    def test_fetches_unknown_tags(self, mock_cls):
        self.entry.project_id = None
        self.entry.tag_ids = [50, 51]
        self.entry.save()
        mock_cls.return_value.get_projects.return_value = []
        mock_cls.return_value.get_tags.return_value = [
            {"id": 50, "name": "a"}, {"id": 51, "name": "b"},
        ]
        _refresh_unknown_metadata(self.entry)
        self.assertEqual(TogglTag.objects.filter(user=self.user, toggl_id__in=[50, 51]).count(), 2)

    @patch("sync.tasks.TogglService")
    def test_skips_when_known(self, mock_cls):
        TogglProject.objects.create(user=self.user, toggl_id=999, workspace=self.ws, name="K")
        self.entry.tag_ids = []
        self.entry.save()
        _refresh_unknown_metadata(self.entry)
        mock_cls.assert_not_called()

    def test_skips_without_toggl_token(self):
        self.user.credentials.toggl_api_token = ""
        self.user.credentials.save()
        _refresh_unknown_metadata(self.entry)

    @patch("sync.tasks.TogglService")
    def test_tolerates_api_errors(self, mock_cls):
        mock_cls.return_value.get_projects.side_effect = TogglAPIError("fail")
        mock_cls.return_value.get_tags.side_effect = TogglAPIError("fail")
        _refresh_unknown_metadata(self.entry)


class ApplyColorToEntryTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("testuser", password="pass")
        self.user.credentials.gauth_credentials_json = '{"token": "t"}'
        self.user.credentials.save()
        self.entry = TogglTimeEntry.objects.create(
            user=self.user, toggl_id=500, description="C",
            start_time=timezone.now() - timedelta(hours=1),
            end_time=timezone.now(), synced=True,
        )

    def test_skips_missing_or_unsynced(self):
        apply_color_to_entry(99999, "5")
        self.entry.synced = False
        self.entry.save()
        apply_color_to_entry(self.entry.id, "5")

    @GCAL
    def test_updates_color(self, mock_cls):
        gcal = _make_gcal(mock_cls, {"id": "evt1"})
        apply_color_to_entry(self.entry.id, "5")
        gcal.update_event.assert_called_once_with(calendar_id="cal_id", event_id="evt1", color_id="5")

    @GCAL
    def test_marks_unsynced_when_missing(self, mock_cls):
        _make_gcal(mock_cls)
        apply_color_to_entry(self.entry.id, "5")
        self.entry.refresh_from_db()
        self.assertFalse(self.entry.synced)


class ValidateSyncedEventsTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("testuser", password="pass")
        self.user.credentials.gauth_credentials_json = '{"token": "t"}'
        self.user.credentials.save()

    def _entry(self, toggl_id, desc="Task"):
        return TogglTimeEntry.objects.create(
            user=self.user, toggl_id=toggl_id, description=desc,
            start_time=timezone.now() - timedelta(hours=1),
            end_time=timezone.now(), synced=True,
        )

    @GCAL
    def test_marks_unsynced_when_missing(self, mock_cls):
        entry = self._entry(601)
        _make_gcal(mock_cls)
        validate_synced_events()
        entry.refresh_from_db()
        self.assertFalse(entry.synced)

    @GCAL
    def test_marks_unsynced_on_summary_mismatch(self, mock_cls):
        entry = self._entry(602, "Right")
        _make_gcal(mock_cls, {"id": "e", "summary": "Wrong"})
        validate_synced_events()
        entry.refresh_from_db()
        self.assertFalse(entry.synced)

    @GCAL
    def test_keeps_synced_when_matching(self, mock_cls):
        entry = self._entry(603, "Match")
        _make_gcal(mock_cls, {"id": "e", "summary": "Match"})
        validate_synced_events()
        entry.refresh_from_db()
        self.assertTrue(entry.synced)

    @GCAL
    def test_skips_disconnected_user(self, mock_cls):
        entry = self._entry(604)
        self.user.credentials.gauth_credentials_json = ""
        self.user.credentials.save()
        validate_synced_events()
        entry.refresh_from_db()
        self.assertTrue(entry.synced)
        mock_cls.assert_not_called()

    @GCAL
    def test_skips_pending_deletion(self, mock_cls):
        entry = self._entry(605)
        entry.pending_deletion = True
        entry.save()
        validate_synced_events()
        mock_cls.assert_not_called()

    @GCAL
    def test_tolerates_per_entry_error(self, mock_cls):
        e1 = self._entry(606, "First")
        e2 = self._entry(607, "Second")
        gcal = _make_gcal(mock_cls)

        def side_effect(calendar_id, ical_uid):
            if ical_uid == e1.gcal_event_id:
                raise GoogleCalendarError("transient")
            return {"id": "e", "summary": "Second"}

        gcal.find_event_by_ical_uid.side_effect = side_effect
        validate_synced_events()
        e1.refresh_from_db()
        e2.refresh_from_db()
        self.assertTrue(e1.synced)
        self.assertTrue(e2.synced)


class GetGcalDataTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("testuser", password="pass")
        self.ws = TogglWorkspace.objects.create(user=self.user, toggl_id=1, name="WS")
        self.now = timezone.now()

    def _entry(self, toggl_id, **kwargs):
        defaults = dict(user=self.user, toggl_id=toggl_id,
                        start_time=self.now - timedelta(hours=1),
                        end_time=self.now, description="Work")
        defaults.update(kwargs)
        return TogglTimeEntry.objects.create(**defaults)

    def test_basic_fields(self):
        e = self._entry(700)
        data = e.get_gcal_data()
        self.assertEqual(data["summary"], "Work")
        self.assertEqual(data["event_id"], "toggl700")
        self.assertIn("Toggl Entry: 700", data["description"])

    def test_project_and_tags_in_description(self):
        TogglProject.objects.create(user=self.user, toggl_id=10, workspace=self.ws, name="ProjX")
        TogglTag.objects.create(user=self.user, toggl_id=20, workspace=self.ws, name="urgent")
        e = self._entry(701, project_id=10, tag_ids=[20])
        data = e.get_gcal_data()
        self.assertIn("Project: ProjX", data["description"])
        self.assertIn("Tags: urgent", data["description"])

    def test_running_entry_grey_and_1min_end(self):
        e = self._entry(702, end_time=None, start_time=self.now)
        data = e.get_gcal_data(color_id="5")
        self.assertEqual(data["color_id"], "8")
        self.assertEqual(data["end"], self.now + timedelta(minutes=1))

    def test_finished_entry_uses_color(self):
        e = self._entry(703)
        data = e.get_gcal_data(color_id="5")
        self.assertEqual(data["color_id"], "5")

    def test_gcal_event_id_format(self):
        e = self._entry(12345)
        self.assertEqual(e.gcal_event_id, "toggl12345")


class ResolveColorTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("testuser", password="pass")
        self.org = TogglOrganization.objects.create(user=self.user, toggl_id=1, name="Org")
        self.ws = TogglWorkspace.objects.create(user=self.user, toggl_id=10, name="WS", organization=self.org)
        self.project = TogglProject.objects.create(user=self.user, toggl_id=100, workspace=self.ws, name="P")

    def _map(self, etype, eid, color, order):
        EntityColorMapping.objects.create(
            user=self.user, entity_type=etype, entity_id=eid,
            entity_name="x", color_name=color, process_order=order,
        )

    def test_returns_none_with_no_mappings(self):
        self.assertIsNone(EntityColorMapping.resolve_color(self.user, project_id=100))

    def test_tag_over_project(self):
        self._map("project", 100, "Sage", 2)
        self._map("tag", 50, "Tomato", 1)
        self.assertEqual(EntityColorMapping.resolve_color(self.user, project_id=100, tag_ids=[50]), "11")

    def test_project_mapping(self):
        self._map("project", 100, "Blueberry", 1)
        self.assertEqual(EntityColorMapping.resolve_color(self.user, project_id=100), "9")

    def test_workspace_fallback(self):
        self._map("workspace", 10, "Peacock", 1)
        self.assertEqual(EntityColorMapping.resolve_color(self.user, project_id=100), "7")

    def test_organization_fallback(self):
        self._map("organization", 1, "Basil", 1)
        self.assertEqual(EntityColorMapping.resolve_color(self.user, project_id=100), "10")

    def test_tag_process_order(self):
        self._map("tag", 50, "Sage", 10)
        self._map("tag", 51, "Tomato", 1)
        self.assertEqual(EntityColorMapping.resolve_color(self.user, tag_ids=[50, 51]), "11")

    def test_no_args_returns_none(self):
        self.assertIsNone(EntityColorMapping.resolve_color(self.user))
