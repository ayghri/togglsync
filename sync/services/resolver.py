"""Calendar resolver service for priority-based calendar assignment."""

import logging
from dataclasses import dataclass

from django.contrib.auth.models import User

from sync.models import GoogleCal
from sync.models import TogglWorkspace
from sync.models import TogglTag
from sync.models import EntityToCalMapping

logger = logging.getLogger(__name__)


@dataclass
class ResolvedCalendar:
    """Result of calendar resolution with optional color."""

    calendar: GoogleCal
    color_id: str | None = None


class CalendarResolver:
    """
    Determines which calendar a time entry should go to based on priority.

    Priority order (highest to lowest):
    1. Tags - if any tag has a mapping, use that calendar
    2. Project - if project has a mapping
    3. Workspace - if workspace has a mapping
    4. Organization - if organization has a mapping
    5. Default - fall back to default calendar
    """

    def __init__(self, user: User):
        """
        Initialize the resolver for a specific user.

        Args:
            user: The Django user whose calendars and mappings to use
        """
        self.user = user

    def resolve(self, time_entry: dict) -> ResolvedCalendar | None:
        """
        Resolve which calendar a time entry should be synced to.

        Args:
            time_entry: Dict containing time entry data with keys like
                       'tag_ids', 'project_id', 'workspace_id'

        Returns:
            ResolvedCalendar with calendar and optional color, or None
        """
        # 1. Check tags (highest priority)
        tag_ids = time_entry.get("tag_ids") or time_entry.get("tags", [])
        if tag_ids:
            # Handle case where tags might be tag names instead of IDs
            if tag_ids and isinstance(tag_ids[0], str):
                # Tags are names, we need to look them up

                tag_objects = TogglTag.objects.filter(
                    user=self.user, name__in=tag_ids
                )
                tag_ids = [t.toggl_id for t in tag_objects]

            if tag_ids:
                mapping = (
                    EntityToCalMapping.objects.filter(
                        user=self.user,
                        entity_type=EntityToCalMapping.EntityType.TAG,
                        entity_id__in=tag_ids,
                    )
                    .select_related("gcal")
                    .order_by("process_order")
                    .first()
                )
                if mapping:
                    logger.debug(
                        f'Resolved to calendar "{mapping.gcal.name}" via tag mapping'
                    )
                    return ResolvedCalendar(
                        calendar=mapping.gcal,
                        color_id=mapping.get_color_id(),
                    )

        # 2. Check project
        project_id = time_entry.get("project_id") or time_entry.get("pid")
        if project_id:
            mapping = (
                EntityToCalMapping.objects.filter(
                    user=self.user,
                    entity_type=EntityToCalMapping.EntityType.PROJECT,
                    entity_id=project_id,
                )
                .select_related("gcal")
                .first()
            )
            if mapping:
                logger.debug(
                    f'Resolved to calendar "{mapping.gcal.name}" via project mapping'
                )
                return ResolvedCalendar(
                    calendar=mapping.gcal, color_id=mapping.get_color_id()
                )

        # 3. Check workspace
        workspace_id = time_entry.get("workspace_id") or time_entry.get("wid")
        if workspace_id:
            mapping = (
                EntityToCalMapping.objects.filter(
                    user=self.user,
                    entity_type=EntityToCalMapping.EntityType.WORKSPACE,
                    entity_id=workspace_id,
                )
                .select_related("gcal")
                .first()
            )
            if mapping:
                logger.debug(
                    f'Resolved to calendar "{mapping.gcal.name}" via workspace mapping'
                )
                return ResolvedCalendar(
                    calendar=mapping.gcal, color_id=mapping.get_color_id()
                )

            # 4. Check organization (look up workspace -> org)
            workspace = TogglWorkspace.objects.filter(
                user=self.user, toggl_id=workspace_id
            ).first()
            if workspace and workspace.organization_id:
                mapping = (
                    EntityToCalMapping.objects.filter(
                        user=self.user,
                        entity_type=EntityToCalMapping.EntityType.ORGANIZATION,
                        entity_id=workspace.organization_id,
                    )
                    .select_related("gcal")
                    .first()
                )
                if mapping:
                    logger.debug(
                        f'Resolved to calendar "{mapping.gcal.name}" via organization mapping'
                    )
                    return ResolvedCalendar(
                        calendar=mapping.gcal,
                        color_id=mapping.get_color_id(),
                    )

        # 5. Default calendar (no color)
        default_calendar = GoogleCal.get_default_for_user(self.user)
        if default_calendar:
            logger.debug(
                f'Resolved to default calendar "{default_calendar.name}"'
            )
            return ResolvedCalendar(calendar=default_calendar, color_id=None)

        logger.warning(
            f"No default calendar configured for user {self.user.username}"
        )
        return None
