"""Google Calendar API client service."""

import json
import logging
from datetime import datetime

from django.conf import settings
from django.contrib.auth.models import User
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


from sync.models import UserCredentials

logger = logging.getLogger(__name__)


class GoogleCalendarError(Exception):
    """Exception raised for Google Calendar API errors."""



class GoogleCalendarService:
    """Client for interacting with the Google Calendar API."""

    def __init__(self, user: User):
        """
        Initialize the Google Calendar service.

        Args:
            user: Django User to load credentials from DB
        """
        self.user = user
        self.scopes = settings.GOOGLE_CALENDAR_SCOPES
        self.credentials = self._load_from_user(user)
        self._refresh_maybe()
        self.service = build("calendar", "v3", credentials=self.credentials)
        self.timezone = self._get_user_creds().timezone

    def _get_user_creds(self) -> UserCredentials:
        return self.user.credentials

    def _load_from_user(self, user: User):
        """Load credentials from database for a user."""

        user_creds = self._get_user_creds()

        creds = Credentials.from_authorized_user_info(
            json.loads(user_creds.gauth_credentials_json), self.scopes
        )

        return creds

    def _refresh_maybe(self):
        """Refresh credentials if they're about to expire."""

        if self.credentials.expired:
            logger.info(
                f"Refreshing expired Google credentials for {self.user.username}"
            )
            self.credentials.refresh(Request())
            user_creds = self._get_user_creds()

            user_creds.gauth_credentials_json = self.credentials.to_json()
            user_creds.save(
                update_fields=["gauth_credentials_json", "updated_at"]
            )

    def ensure_toggl_calendar(self) -> str:
        """
        Ensure the Toggl calendar exists.

        With calendar.app.created scope, the app owns the calendar. Even if
        the user removes it from their Google Calendar UI, the API can still
        access it (soft delete). If it's truly gone, we recreate it.

        Returns:
            The Google Calendar ID for the Toggl calendar.

        Raises:
            GoogleCalendarError: If credentials are invalid or expired.
        """
        # Reload from DB to catch disconnect/reconnect between task queue and execution
        self.user.refresh_from_db()
        user_creds = self._get_user_creds()

        if not user_creds.is_connected:
            raise GoogleCalendarError(
                f"Google Calendar not connected for {self.user.username}"
            )

        # If we have a stored calendar ID, verify it still exists
        if user_creds.google_calendar_id:
            if self.get_calendar(user_creds.google_calendar_id):
                return user_creds.google_calendar_id
            logger.warning(
                f"Stored calendar {user_creds.google_calendar_id} no longer exists, "
                f"recreating"
            )

        # Create a new "Toggl" calendar
        self._refresh_maybe()
        cal = self.service.calendars().insert(
            body={
                "summary": "Toggl",
                "description": "Time entries synced from Toggl Track",
                "timeZone": self.timezone,
            }
        ).execute()

        calendar_id = cal["id"]
        user_creds.google_calendar_id = calendar_id
        user_creds.save(update_fields=["google_calendar_id", "updated_at"])
        logger.info(f"Created Toggl calendar {calendar_id} for {self.user.username}")

        return calendar_id

    def get_calendar(self, calendar_id: str) -> dict | None:
        """Get a calendar by ID."""
        self._refresh_maybe()
        try:
            return (
                self.service.calendars().get(calendarId=calendar_id).execute()
            )
        except HttpError as e:
            if e.resp.status == 404:
                return None
            raise GoogleCalendarError(f"Failed to get calendar: {e}") from e

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
        """Create a calendar event."""
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

        # Use iCalUID for stable external ID (allows lookup by toggl_entry_id)
        if event_id:
            event_body["iCalUID"] = event_id

        # Set event color (1-11)
        if color_id:
            event_body["colorId"] = color_id

        try:
            return (
                self.service.events()
                .insert(calendarId=calendar_id, body=event_body)
                .execute()
            )
        except HttpError as e:
            # If event already exists (409), try to find and return it
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
        """Update an existing calendar event."""
        self._refresh_maybe()

        try:
            # Get existing event
            event = (
                self.service.events()
                .get(calendarId=calendar_id, eventId=event_id)
                .execute()
            )

            # Update fields if provided
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
        """Delete a calendar event."""
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

    def get_event(self, calendar_id: str, event_id: str) -> dict | None:
        """Get a single event by ID."""
        self._refresh_maybe()

        try:
            return (
                self.service.events()
                .get(calendarId=calendar_id, eventId=event_id)
                .execute()
            )
        except HttpError as e:
            if e.resp.status == 404:
                return None
            raise GoogleCalendarError(f"Failed to get event: {e}") from e

    def event_exists(self, calendar_id: str, event_id: str) -> bool:
        """Check if an event exists in a calendar."""
        return self.get_event(calendar_id, event_id) is not None

    def find_event_by_ical_uid(
        self, calendar_id: str, ical_uid: str
    ) -> dict | None:
        """Find an event by its iCalUID."""
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
