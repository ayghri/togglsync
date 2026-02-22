from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

from .utils import UserScopedModel


class UserCredentials(UserScopedModel, models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="credentials"
    )
    toggl_api_token = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Toggl Token",
    )
    gauth_credentials_json = models.TextField(
        blank=True,
        default="",
        help_text="Google OAuth",
    )
    google_calendar_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Auto-created Toggl calendar ID",
    )
    timezone = models.CharField(max_length=50, default="UTC")
    last_toggl_metadata_sync = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Credentials"
        verbose_name_plural = "Credentials"

    def __str__(self):
        api_token = str(self.toggl_api_token)
        masked = (
            api_token[:4] + "***" + api_token[-4:]
            if self.toggl_api_token
            else "Not set"
        )
        return f"Toggl Config for {self.user.username} (token: {masked})"

    @property
    def is_connected(self) -> bool:
        """Check if OAuth tokens are present."""
        return bool(self.gauth_credentials_json)


class TogglOrganization(UserScopedModel, models.Model):
    """Cached Toggl organization per user."""

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="toggl_orgs",
    )
    toggl_id = models.BigIntegerField()
    name = models.CharField(max_length=255)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Toggl Organization"
        unique_together = ["user", "toggl_id"]

    def __str__(self):
        return str(self.name)


class TogglWorkspace(UserScopedModel, models.Model):
    """Cached Toggl workspace with webhook configuration per user."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="toggl_workspaces"
    )
    toggl_id = models.BigIntegerField()

    organization = models.ForeignKey(
        TogglOrganization,
        on_delete=models.DO_NOTHING,
        related_name="workspaces",
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=255)

    # Webhook configuration (per workspace per user)
    webhook_token = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        help_text="Unique token for webhook URL routing",
    )
    webhook_subscription_id = models.BigIntegerField(null=True, blank=True)
    webhook_secret = models.CharField(max_length=255, null=True, blank=True)
    webhook_enabled = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Toggl Workspace"
        unique_together = ["user", "toggl_id"]

    def __str__(self):
        webhook_status = " [webhook]" if self.webhook_enabled else ""
        return f"{self.name}{webhook_status}"

    @property
    def has_webhook(self):
        return bool(self.webhook_subscription_id)


class TogglProject(UserScopedModel, models.Model):
    """Cached Toggl project per user."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="toggl_projects"
    )
    toggl_id = models.BigIntegerField()
    workspace = models.ForeignKey(
        TogglWorkspace, on_delete=models.DO_NOTHING, related_name="projects"
    )
    name = models.CharField(max_length=255)
    color = models.CharField(max_length=20, null=True, blank=True)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Toggl Project"
        unique_together = ["user", "toggl_id"]

    def __str__(self):
        status = "" if self.active else " (inactive)"
        return f"{self.name}{status}"


class TogglTag(UserScopedModel, models.Model):
    """Cached Toggl tag per user."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="toggl_tags"
    )
    toggl_id = models.BigIntegerField()
    workspace = models.ForeignKey(
        TogglWorkspace,
        on_delete=models.DO_NOTHING,
        related_name="tags",
    )
    name = models.CharField(max_length=255)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Toggl Tag"
        unique_together = ["user", "toggl_id"]

    def __str__(self):
        return self.name


class TogglTimeEntry(UserScopedModel, models.Model):
    """Tracks time entries from Toggl webhooks"""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="synced_entries"
    )
    toggl_id = models.BigIntegerField()

    # Entry data from webhook
    description = models.TextField(blank=True, default="")
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    project_id = models.BigIntegerField(null=True, blank=True)
    tag_ids = models.JSONField(default=list)

    # Sync tracking
    synced = models.BooleanField(default=False)
    pending_deletion = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Toggl Entry"
        verbose_name_plural = "Toggl Entries"
        ordering = ["-start_time"]
        unique_together = ["user", "toggl_id"]
        indexes = [
            models.Index(fields=["user", "synced", "updated_at"]),
        ]

    @property
    def gcal_event_id(self) -> str:
        return "toggl" + str(self.toggl_id)

    def get_gcal_data(self) -> dict:
        """
        Prepare all data needed for Google Calendar event creation/update.

        Returns:
            Dictionary with event_id, summary, start, end, description, color_id
        """
        from datetime import timedelta

        # Query database for project name
        project_name = None
        if self.project_id:
            project = TogglProject.objects.filter(
                user=self.user, toggl_id=self.project_id
            ).first()
            if project:
                project_name = project.name

        # Query database for tag names
        tag_names = []
        if self.tag_ids:
            tags = TogglTag.objects.filter(user=self.user, toggl_id__in=self.tag_ids)
            tag_names = [t.name for t in tags]

        # Build event description
        desc_lines = [f"Toggl Entry: {self.toggl_id}"]
        if project_name:
            desc_lines.append(f"Project: {project_name}")
        if tag_names:
            desc_lines.append(f'Tags: {", ".join(tag_names)}')
        event_description = "\n".join(desc_lines)

        # Determine start and end times
        start = self.start_time
        if self.end_time:
            end = self.end_time
        else:
            # Running entry: use start + 1 minute as placeholder
            end = start + timedelta(minutes=1)

        # Grey for running entries, no color for completed
        event_color_id = "8" if not self.end_time else None

        # Build summary
        summary = self.description or "(No description)"

        return {
            "event_id": self.gcal_event_id,
            "summary": summary,
            "start": start,
            "end": end,
            "description": event_description,
            "color_id": event_color_id,
        }

    def __str__(self):
        status = "synced" if self.synced else "pending"
        return f'Entry {self.id}: {self.description[:50] or "(no description)"} ({status})'


def check_unknown_entities(time_entry: dict, user) -> dict:
    """
    Check if a time entry contains unknown entities (project, tags, workspace) for a user.
    Returns a dict of unknown entity types and their IDs.
    """
    unknown = {}

    # Check workspace
    workspace_id = time_entry.get("workspace_id") or time_entry.get("wid")
    if (
        workspace_id
        and not TogglWorkspace.objects
        .filter(user=user, toggl_id=workspace_id)
        .exists()
    ):
        unknown["workspace"] = workspace_id

    # Check project
    project_id = time_entry.get("project_id") or time_entry.get("pid")
    if (
        project_id
        and not TogglProject.objects
        .filter(user=user, toggl_id=project_id)
        .exists()
    ):
        unknown["project"] = project_id

    # Check tags
    tag_ids = time_entry.get("tag_ids", [])
    if tag_ids:
        existing_tags = set(
            TogglTag.objects.filter(
                user=user, toggl_id__in=tag_ids
            ).values_list("toggl_id", flat=True)
        )
        unknown_tags = [t for t in tag_ids if t not in existing_tags]
        if unknown_tags:
            unknown["tags"] = unknown_tags

    return unknown
