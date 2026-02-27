from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class UserCredentials(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="credentials"
    )
    toggl_api_token = models.CharField(max_length=255, blank=True, default="")
    gauth_credentials_json = models.TextField(blank=True, default="")
    google_calendar_id = models.CharField(max_length=255, blank=True, default="")
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
        return bool(self.gauth_credentials_json)


class TogglOrganization(models.Model):
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


class TogglWorkspace(models.Model):
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


class TogglProject(models.Model):
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


class TogglTag(models.Model):
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


class TogglTimeEntry(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="synced_entries"
    )
    toggl_id = models.BigIntegerField()
    description = models.TextField(blank=True, default="")
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    project_id = models.BigIntegerField(null=True, blank=True)
    tag_ids = models.JSONField(default=list)
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
        """Build dict for Google Calendar event creation/update."""
        from datetime import timedelta

        project_name = None
        if self.project_id:
            project = TogglProject.objects.filter(
                user=self.user, toggl_id=self.project_id
            ).first()
            if project:
                project_name = project.name

        tag_names = []
        if self.tag_ids:
            tags = TogglTag.objects.filter(user=self.user, toggl_id__in=self.tag_ids)
            tag_names = [t.name for t in tags]

        desc_lines = [f"Toggl Entry: {self.toggl_id}"]
        if project_name:
            desc_lines.append(f"Project: {project_name}")
        if tag_names:
            desc_lines.append(f'Tags: {", ".join(tag_names)}')
        event_description = "\n".join(desc_lines)

        start = self.start_time
        end = self.end_time or start + timedelta(minutes=1)

        # Grey for running entries, mapped color otherwise
        event_color_id = "8" if not self.end_time else color_id

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


class EntityColorMapping(models.Model):
    class EntityType(models.TextChoices):
        TAG = ("tag", "Tag")
        PROJECT = ("project", "Project")
        WORKSPACE = ("workspace", "Workspace")
        ORGANIZATION = ("organization", "Organization")

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="color_mappings"
    )
    entity_type = models.CharField(max_length=20, choices=EntityType.choices)
    entity_id = models.BigIntegerField()
    entity_name = models.CharField(max_length=255, help_text="for display")
    process_order = models.IntegerField(unique=True, help_text="lower = higher priority")

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
        return self.COLOR_ID_MAP[str(self.color_name)]

    def find_matching_entries(self):
        base_query = TogglTimeEntry.objects.filter(
            user=self.user,
            synced=True,
            pending_deletion=False,
        )

        if self.entity_type == self.EntityType.PROJECT:
            return base_query.filter(project_id=self.entity_id)

        elif self.entity_type == self.EntityType.TAG:
            return base_query.filter(tag_ids__contains=self.entity_id)

        elif self.entity_type == self.EntityType.WORKSPACE:
            project_ids = TogglProject.objects.filter(
                user=self.user,
                workspace__toggl_id=self.entity_id,
            ).values_list("toggl_id", flat=True)
            return base_query.filter(project_id__in=project_ids)

        elif self.entity_type == self.EntityType.ORGANIZATION:
            project_ids = TogglProject.objects.filter(
                user=self.user,
                workspace__organization__toggl_id=self.entity_id,
            ).values_list("toggl_id", flat=True)
            return base_query.filter(project_id__in=project_ids)

        return base_query.none()

    class Meta:
        unique_together = ["user", "entity_type", "entity_id"]
        ordering = ["process_order"]
        verbose_name = "Color Mapping"
        verbose_name_plural = "Color Mappings"

    def __str__(self):
        return f"{self.entity_type}: {self.entity_name} -> {self.color_name}"


def resolve_color(user, time_entry: dict) -> str | None:
    """Resolve event color by priority: tags > project > workspace > organization."""
    ECM = EntityColorMapping

    tag_ids = time_entry.get("tag_ids") or time_entry.get("tags", [])
    if tag_ids:
        if isinstance(tag_ids[0], str):
            tag_objects = TogglTag.objects.filter(user=user, name__in=tag_ids)
            tag_ids = [t.toggl_id for t in tag_objects]
        if tag_ids:
            mapping = (
                ECM.objects.filter(
                    user=user,
                    entity_type=ECM.EntityType.TAG,
                    entity_id__in=tag_ids,
                )
                .order_by("process_order")
                .first()
            )
            if mapping:
                return mapping.get_color_id()

    project_id = time_entry.get("project_id") or time_entry.get("pid")
    if project_id:
        mapping = ECM.objects.filter(
            user=user,
            entity_type=ECM.EntityType.PROJECT,
            entity_id=project_id,
        ).first()
        if mapping:
            return mapping.get_color_id()

    workspace_id = time_entry.get("workspace_id") or time_entry.get("wid")
    if workspace_id:
        mapping = ECM.objects.filter(
            user=user,
            entity_type=ECM.EntityType.WORKSPACE,
            entity_id=workspace_id,
        ).first()
        if mapping:
            return mapping.get_color_id()

        ws = TogglWorkspace.objects.filter(
            user=user, toggl_id=workspace_id
        ).first()
        if ws and ws.organization_id:
            mapping = ECM.objects.filter(
                user=user,
                entity_type=ECM.EntityType.ORGANIZATION,
                entity_id=ws.organization.toggl_id,
            ).first()
            if mapping:
                return mapping.get_color_id()

    return None


def check_unknown_entities(time_entry: dict, user) -> dict:
    """Return dict of entity types/IDs not yet in the DB for this user."""
    unknown = {}

    workspace_id = time_entry.get("workspace_id") or time_entry.get("wid")
    if (
        workspace_id
        and not TogglWorkspace.objects
        .filter(user=user, toggl_id=workspace_id)
        .exists()
    ):
        unknown["workspace"] = workspace_id

    project_id = time_entry.get("project_id") or time_entry.get("pid")
    if (
        project_id
        and not TogglProject.objects
        .filter(user=user, toggl_id=project_id)
        .exists()
    ):
        unknown["project"] = project_id

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
