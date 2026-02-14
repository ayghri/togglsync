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


class GoogleCal(UserScopedModel, models.Model):
    """Represents a Google Calendar that can receive time entries."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="calendars"
    )
    name = models.CharField(max_length=255)
    calendar_id = models.CharField(max_length=255)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_default", "name"]
        unique_together = ["user", "calendar_id"]
        verbose_name = "Calendar"

    def __str__(self):
        default_marker = " (DEFAULT)" if self.is_default else ""
        return f"{self.name}{default_marker}"

    def save(self, *args, **kwargs):
        # If this is being set as default, unset other defaults for this user
        if self.is_default:
            GoogleCal.objects.filter(user=self.user, is_default=True).exclude(
                pk=self.pk
            ).update(is_default=False)
        super().save(*args, **kwargs)

    @classmethod
    def get_default_for_user(cls, user):
        """Get the default calendar for a user or None."""
        return cls.objects.filter(user=user, is_default=True).first()



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
    calendar = models.ForeignKey(
        GoogleCal, on_delete=models.DO_NOTHING, related_name="synced_entries",
        null=True, blank=True
    )

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

    def get_gcal_data(self, color_id: str | None = None) -> dict:
        """
        Prepare all data needed for Google Calendar event creation/update.

        Args:
            color_id: Optional color ID for the event (from calendar mapping)

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

        # Determine color (grey for running entries)
        if not self.end_time:
            event_color_id = "8"  # Grey for running entries
        else:
            event_color_id = color_id

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

class EntityToCalMapping(UserScopedModel, models.Model):

    class EntityType(models.TextChoices):
        TAG = ("tag", "Tag")
        PROJECT = ("project", "Project")
        WORKSPACE = ("workspace", "Workspace")
        ORGANIZATION = ("organization", "Organization")

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="calendar_mappings"
    )
    gcal = models.ForeignKey(
        GoogleCal, on_delete=models.CASCADE, related_name="mappings"
    )
    entity_type = models.CharField(max_length=20, choices=EntityType.choices)
    entity_id = models.BigIntegerField()
    entity_name = models.CharField(max_length=255, help_text="for display")
    process_order = models.IntegerField(unique=True, help_text="lower = prior")

    EVENT_COLORS = {
        "Lavender": "#a4bdfc",
        "Sage": "#7ae7bf",
        "Grape": "#dbadff",
        "Flamingo": "#ff887c",
        "Banana": "#fbd75b",
        "Tangerine": "#ffb878",
        "Peacock": "#46d6db",
        "Graphite": "#e1e1e1",
        "Blueberry": "#5484ed",
        "Basil": "#51b749",
        "Tomato": "#dc2127",
    }

    # Map color names to Google Calendar color IDs
    COLOR_ID_MAP = {
        "Lavender": "1",
        "Sage": "2",
        "Grape": "3",
        "Flamingo": "4",
        "Banana": "5",
        "Tangerine": "6",
        "Peacock": "7",
        "Graphite": "8",
        "Blueberry": "9",
        "Basil": "10",
        "Tomato": "11",
    }

    COLOR_CHOICES = list(EVENT_COLORS.items())

    color_name = models.CharField(
        max_length=20,
        choices=COLOR_CHOICES,
        default="Lavender",
        help_text="Color for calendar events",
    )

    def get_color_hex(self):
        return self.EVENT_COLORS[str(self.color_name)]

    def get_color_id(self):
        """Get Google Calendar color ID for this mapping's color."""
        return self.COLOR_ID_MAP[str(self.color_name)]

    def find_matching_entries(self):
        """Find time entries that match this mapping's entity."""
        base_query = TogglTimeEntry.objects.filter(
            user=self.user,
            synced=True,
            pending_deletion=False
        )

        if self.entity_type == self.EntityType.PROJECT:
            return base_query.filter(project_id=self.entity_id)

        elif self.entity_type == self.EntityType.TAG:
            # JSONField contains check
            return base_query.filter(tag_ids__contains=self.entity_id)

        elif self.entity_type == self.EntityType.WORKSPACE:
            # Find all projects in this workspace
            project_ids = TogglProject.objects.filter(
                user=self.user,
                workspace__toggl_id=self.entity_id
            ).values_list('toggl_id', flat=True)
            return base_query.filter(project_id__in=project_ids)

        elif self.entity_type == self.EntityType.ORGANIZATION:
            # Find all projects in workspaces belonging to this org
            project_ids = TogglProject.objects.filter(
                user=self.user,
                workspace__organization__toggl_id=self.entity_id
            ).values_list('toggl_id', flat=True)
            return base_query.filter(project_id__in=project_ids)

        return base_query.none()

    class Meta:
        unique_together = ["user", "entity_type", "entity_id"]
        ordering = ["entity_type", "process_order", "entity_name"]
        verbose_name = "Mapping"
        verbose_name_plural = "Mappings"




def build_event_description(
    entry_id: int,
    project_name: str | None = None,
    tag_names: list[str] | None = None,
    billable: bool = False,
) -> str:
    """
    Build a detailed description for the calendar event.

    Args:
        entry_id: Toggl time entry ID
        project_name: Name of the project (if any)
        tag_names: List of tag names (if any)
        billable: Whether the entry is billable

    Returns:
        Formatted description string
    """
    lines = []

    # Add Toggl entry ID for reference
    lines.append(f"Toggl Entry: {entry_id}")

    # Add project if available
    if project_name:
        lines.append(f"Project: {project_name}")

    # Add tags if available
    if tag_names:
        lines.append(f'Tags: {", ".join(tag_names)}')

    # Add billable status
    if billable:
        lines.append("Billable: Yes")

    return "\n".join(lines)


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
