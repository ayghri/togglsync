import json
import logging
from datetime import datetime

from django.conf import settings
from django.contrib.auth.models import User
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from sync.models import UserCredentials

logger = logging.getLogger(__name__)


class GoogleCalendarError(Exception):
    pass


class GoogleCalendarService:
    def __init__(self, user: User):
        self.user = user
        self.scopes = settings.GOOGLE_CALENDAR_SCOPES
        self.credentials = self._load_from_user(user)
        self._refresh_maybe()
        self.service = build("calendar", "v3", credentials=self.credentials)
        self.timezone = self._get_user_creds().timezone

    def _get_user_creds(self) -> UserCredentials:
        return self.user.credentials

    def _load_from_user(self, user: User):
        user_creds = self._get_user_creds()
        return Credentials.from_authorized_user_info(
            json.loads(user_creds.gauth_credentials_json), self.scopes
        )

    def _refresh_maybe(self):
        if not self.credentials.expired:
            return

        logger.info(f"Refreshing expired Google credentials for {self.user.username}")
        try:
            self.credentials.refresh(Request())
        except RefreshError as e:
            logger.error(
                f"Google OAuth refresh failed for {self.user.username}: {e}. "
                f"Could be revoked token or transient network issue. "
                f"Tasks will retry after delay."
            )
            raise GoogleCalendarError(
                f"Google OAuth refresh failed for {self.user.username}: {e}"
            ) from e

        user_creds = self._get_user_creds()
        user_creds.gauth_credentials_json = self.credentials.to_json()
        user_creds.save(update_fields=["gauth_credentials_json", "updated_at"])

    def ensure_toggl_calendar(self) -> str:
        """Return Toggl calendar ID, creating one if needed."""
        self.user.refresh_from_db()
        user_creds = self._get_user_creds()

        if not user_creds.is_connected:
            raise GoogleCalendarError(
                f"Google Calendar not connected for {self.user.username}"
            )

        if user_creds.google_calendar_id:
            return user_creds.google_calendar_id

        return self._create_toggl_calendar()

    def _create_toggl_calendar(self) -> str:
        self._refresh_maybe()
        cal = self.service.calendars().insert(
            body={
                "summary": "Toggl",
                "description": "Time entries synced from Toggl Track",
                "timeZone": self.timezone,
            }
        ).execute()

        calendar_id = cal["id"]
        user_creds = self._get_user_creds()
        user_creds.google_calendar_id = calendar_id
        user_creds.save(update_fields=["google_calendar_id", "updated_at"])
        logger.info(f"Created Toggl calendar {calendar_id} for {self.user.username}")

        return calendar_id

    def create_event(
        self,
        calendar_id: str,
        summary: str,
        start: datetime,
        end: datetime,
        description: str = "",
        event_id: str | None = None,
        color_id: str | None = None,
    ) -> dict:
        self._refresh_maybe()
        event_body = {
            "summary": summary,
            "description": description,
            "start": {
                "dateTime": start.isoformat(),
                "timeZone": self.timezone,
            },
            "end": {
                "dateTime": end.isoformat(),
                "timeZone": self.timezone,
            },
        }

        if event_id:
            event_body["iCalUID"] = event_id
        if color_id:
            event_body["colorId"] = color_id

        try:
            return (
                self.service.events()
                .insert(calendarId=calendar_id, body=event_body)
                .execute()
            )
        except HttpError as e:
            if e.resp.status == 409 and event_id:
                logger.warning(f"Event with iCalUID {event_id} already exists, finding it")
                existing = self.find_event_by_ical_uid(calendar_id, event_id)
                if existing:
                    logger.info(f"Found existing event: {existing['id']}")
                    return existing
            raise GoogleCalendarError(f"Failed to create event: {e}") from e

    def update_event(
        self,
        calendar_id: str,
        event_id: str,
        summary: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        description: str | None = None,
        color_id: str | None = None,
    ) -> dict:
        self._refresh_maybe()
        try:
            event = (
                self.service.events()
                .get(calendarId=calendar_id, eventId=event_id)
                .execute()
            )

            if summary is not None:
                event["summary"] = summary
            if description is not None:
                event["description"] = description
            if start is not None:
                event["start"] = {
                    "dateTime": start.isoformat(),
                    "timeZone": self.timezone,
                }
            if end is not None:
                event["end"] = {
                    "dateTime": end.isoformat(),
                    "timeZone": self.timezone,
                }
            if color_id is not None:
                event["colorId"] = color_id

            return (
                self.service.events()
                .update(calendarId=calendar_id, eventId=event_id, body=event)
                .execute()
            )
        except HttpError as e:
            raise GoogleCalendarError(f"Failed to update event: {e}") from e

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self._refresh_maybe()

        try:
            self.service.events().delete(
                calendarId=calendar_id, eventId=event_id
            ).execute()
        except HttpError as e:
            if e.resp.status == 404:
                logger.warning(
                    f"Event {event_id} not found, may already be deleted"
                )
                return
            raise GoogleCalendarError(f"Failed to delete event: {e}") from e

    def find_event_by_ical_uid(
        self, calendar_id: str, ical_uid: str
    ) -> dict | None:
        self._refresh_maybe()

        try:
            result = (
                self.service.events()
                .list(calendarId=calendar_id, iCalUID=ical_uid)
                .execute()
            )
            items = result.get("items", [])
            return items[0] if items else None
        except HttpError as e:
            raise GoogleCalendarError(f"Failed to find event: {e}") from e
