"""Toggl Track API client service."""

import logging

import requests
from django.conf import settings


logger = logging.getLogger(__name__)


class TogglAPIError(Exception):
    """Exception raised for Toggl API errors."""


class TogglService:
    """Client for interacting with the Toggl Track API."""

    def __init__(self, api_token: str):
        self.api_token = api_token
        self.session = requests.Session()
        self.session.auth = (self.api_token, "api_token")
        self.session.headers.update({"Content-Type": "application/json"})

    def _request(self, method: str, url: str, **kwargs):
        """Make an API request and handle errors."""
        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()

            if response.status_code == 204:
                return None

            return response.json()
        except requests.exceptions.HTTPError as e:
            logger.error(
                f"Toggl API error: {e.response.status_code} - {e.response.text}"
            )
            raise e
        except requests.exceptions.RequestException as e:
            logger.error(f"Toggl API request failed: {e}")
            raise e

    def _request_api(self, method: str, url: str, **kwargs):
        """Make an API request and handle errors."""
        return self._request(
            method, f"{settings.TOGGL_API_ENDPOINT}/{url}", **kwargs
        )

    def _request_webhook_api(self, method: str, url: str, **kwargs):
        """Make an API request and handle errors."""
        return self._request(
            method, f"{settings.TOGGL_WEBHOOK_API_ENDPOINT}/{url}", **kwargs
        )

    # =========================================================================
    # User & Metadata
    # =========================================================================

    def get_me(self, with_related_data: bool = False):
        """Get current user info."""
        params = {}
        if with_related_data:
            params["with_related_data"] = "true"
        return self._request_api("GET", "me", params=params)

    def get_organizations(self):
        """Get user's organizations."""
        return self._request_api("GET", "me/organizations")

    def get_workspaces(self):
        """Get user's workspaces."""
        return self._request_api("GET", "me/workspaces")

    def get_projects(self, workspace_id: int) -> list[dict]:
        """Get all projects in a workspace."""
        projects = []
        page = 1
        per_page = 200

        while True:
            result_data = self._request_api(
                "GET",
                f"workspaces/{workspace_id}/projects",
                params={"page": page, "per_page": per_page},
            )
            if not result_data:
                break

            projects.extend(result_data)

            if len(result_data) < per_page:
                break
            page += 1

        return projects

    def get_tags(self, workspace_id: int):
        """Get all tags in a workspace."""
        return self._request_api("GET", f"workspaces/{workspace_id}/tags")

    # =========================================================================
    # Time Entries
    # =========================================================================

    def get_time_entries(
        self, start_date: str | None = None, end_date: str | None = None
    ):
        """Get time entries within a date range."""
        params = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        return self._request_api("GET", "me/time_entries", params=params)

    def get_time_entry(self, entry_id: int):
        """Get a single time entry by ID."""
        return self._request_api("GET", f"me/time_entries/{entry_id}")

    # =========================================================================
    # Webhooks
    # =========================================================================

    def list_webhooks(self, workspace_id: int):
        """List all webhook subscriptions for a workspace."""
        return self._request_webhook_api("GET", f"subscriptions/{workspace_id}")

    def create_webhook(
        self,
        workspace_id: int,
        callback_url: str,
        description: str = "togglsync",
    ):
        """Create a webhook subscription for time entry events."""
        payload = {
            "description": description,
            "url_callback": callback_url,
            "event_filters": [
                {"entity": "time_entry", "action": "created"},
                {"entity": "time_entry", "action": "updated"},
                {"entity": "time_entry", "action": "deleted"},
            ],
            "enabled": True,
        }

        return self._request_webhook_api(
            "POST",
            f"subscriptions/{workspace_id}",
            json=payload,
        )

    def update_webhook(self, workspace_id: int, subscription_id: int, **kwargs):
        """Update a webhook subscription."""
        return self._request_webhook_api(
            "PUT",
            f"subscriptions/{workspace_id}/{subscription_id}",
            json=kwargs,
        )

    def delete_webhook(self, workspace_id: int, subscription_id: int) -> None:
        """Delete a webhook subscription."""
        self._request_webhook_api(
            "DELETE",
            f"subscriptions/{workspace_id}/{subscription_id}",
        )

    def toggle_webhook(
        self, workspace_id: int, subscription_id: int, enabled: bool
    ):
        """Enable or disable a webhook subscription."""
        return self._request_webhook_api(
            "PATCH",
            f"subscriptions/{workspace_id}/{subscription_id}",
            json={"enabled": enabled},
        )

    def ping_webhook(self, workspace_id: int, subscription_id: int):
        """Send a PING to test webhook endpoint."""
        return self._request_webhook_api(
            "POST",
            f"subscriptions/{workspace_id}/{subscription_id}/ping",
        )
