"""Background tasks for processing webhook events."""

import logging
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.utils import timezone
from django_q.models import Schedule

from .models import TogglTimeEntry
from .models import TogglOrganization
from .models import TogglWorkspace, TogglProject
from .models import TogglTag, check_unknown_entities
from .services import GoogleCalendarService
from .services import TogglService, TogglAPIError
from .services import GoogleCalendarError
from .utils import parse_datetime

logger = logging.getLogger(__name__)


def refresh_metadata_for_workspace(user: User, workspace_id: int):
    """Refresh metadata from Toggl API for a specific workspace and user."""
    creds = user.credentials
    if not creds.toggl_api_token:
        logger.warning(
            f"Cannot refresh metadata: no Toggl API token for user {user.username}"
        )
        return

    try:
        toggl = TogglService(creds.toggl_api_token)

        # Look up workspace object
        workspace = TogglWorkspace.objects.filter(
            user=user, toggl_id=workspace_id
        ).first()
        if not workspace:
            logger.warning(f"Workspace {workspace_id} not found for user {user.username}")
            return

        # Sync projects
        try:
            projects = toggl.get_projects(workspace_id)
            for project in projects:
                TogglProject.objects.update_or_create(
                    user=user,
                    toggl_id=project["id"],
                    defaults={
                        "workspace": workspace,
                        "name": project["name"],
                        "color": project.get("color"),
                        "active": project.get("active", True),
                    },
                )
        except TogglAPIError as e:
            logger.warning(
                f"Failed to sync projects for workspace {workspace_id}: {e}"
            )

        # Sync tags
        try:
            tags = toggl.get_tags(workspace_id)
            if tags:
                for tag in tags:
                    TogglTag.objects.update_or_create(
                        user=user,
                        toggl_id=tag["id"],
                        defaults={
                            "workspace": workspace,
                            "name": tag["name"],
                        },
                    )
        except TogglAPIError as e:
            logger.warning(
                f"Failed to sync tags for workspace {workspace_id}: {e}"
            )

        logger.info(
            f"Refreshed metadata for workspace {workspace_id} (user: {user.username})"
        )

    except Exception as e:
        logger.exception(f"Error refreshing metadata: {e}")


def process_time_entry_event(user_id: int, entry_id: int):
    """
    Process a time entry event from Toggl.

    This task checks if enough time has passed since the last update,
    then syncs to Google Calendar. This minimizes rapid API calls.
    """
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found")
        return

    # Fetch the entry from database
    try:
        entry = TogglTimeEntry.objects.get(user=user, toggl_id=entry_id)
    except TogglTimeEntry.DoesNotExist:
        logger.warning(f"Entry {entry_id} not found in database")
        return

    # Check if enough time has passed since last update
    time_since_update = (timezone.now() - entry.updated_at).total_seconds()
    wait_time = getattr(settings, 'QCLUSTER_WAIT', 60)

    if time_since_update < wait_time:
        # Not enough time has passed, reschedule
        remaining = int(wait_time - time_since_update) + 1
        logger.info(
            f"Entry {entry_id} updated {time_since_update:.1f}s ago, "
            f"waiting {remaining}s more before processing (task deferred)"
        )

        # Update or create scheduled task for this entry
        next_run = timezone.now() + timezone.timedelta(seconds=remaining)
        Schedule.objects.update_or_create(
            name=f"process_entry_{entry_id}",
            defaults={
                "func": "sync.tasks.process_time_entry_event",
                "args": f"({user_id}, {entry_id})",
                "schedule_type": Schedule.ONCE,
                "next_run": next_run,
            }
        )
        logger.debug(f"Deferred processing of entry {entry_id}, scheduled for {next_run}")
        return

    # Check if Google Calendar is connected
    if not user.credentials.is_connected:
        logger.warning(
            f"Skipping entry {entry_id}: Google Calendar not connected for {user.username}"
        )
        return

    logger.info(
        f"Processing entry {entry_id} (user: {user.username}, "
        f"pending_deletion: {entry.pending_deletion})"
    )

    # Build time_entry dict for processing functions
    time_entry = {
        "id": entry.toggl_id,
        "description": entry.description,
        "start": entry.start_time.isoformat(),
        "stop": entry.end_time.isoformat() if entry.end_time else None,
        "project_id": entry.project_id,
        "tag_ids": entry.tag_ids,
    }

    # Check for unknown entities and refresh if needed
    unknown = check_unknown_entities(time_entry, user)
    if unknown:
        logger.info(f"Found unknown entities: {unknown}, refreshing metadata")
        ws_id = time_entry.get("workspace_id") or time_entry.get("wid")
        if ws_id:
            refresh_metadata_for_workspace(user, ws_id)

    try:
        if entry.pending_deletion:
            _handle_deleted(time_entry, user)
        else:
            if entry.synced:
                _handle_updated(time_entry, user)
            else:
                _handle_created(time_entry, user)

        # Mark as synced
        entry.synced = True
        entry.save(update_fields=["synced"])

    except Exception as e:
        logger.exception(f"Error processing entry {entry_id}: {e}")
        raise  # Re-raise so Django-Q marks the task as failed


def _handle_created(time_entry: dict, user: User):
    """Handle a new time entry creation."""
    entry_id = time_entry.get("id")
    if not entry_id:
        logger.warning("Time entry missing ID")
        return

    # Get the database entry
    try:
        db_entry = TogglTimeEntry.objects.get(user=user, toggl_id=entry_id)
    except TogglTimeEntry.DoesNotExist:
        logger.error(f"Entry {entry_id} not found in database")
        return

    gcal = GoogleCalendarService(user=user)
    calendar_id = gcal.ensure_toggl_calendar()

    gcal_data = db_entry.get_gcal_data()
    is_running = not db_entry.end_time

    gcal.create_event(
        calendar_id=calendar_id,
        **gcal_data,
    )

    db_entry.synced = True
    db_entry.save(update_fields=["synced"])

    logger.info(
        f"Created calendar event for entry {entry_id} (user: {user.username}, running: {is_running})"
    )


def _handle_updated(time_entry: dict, user: User):
    """Handle a time entry update."""
    entry_id = time_entry.get("id")
    if not entry_id:
        logger.warning("Time entry missing ID")
        return

    try:
        db_entry = TogglTimeEntry.objects.get(user=user, toggl_id=entry_id)
    except TogglTimeEntry.DoesNotExist:
        logger.error(f"Entry {entry_id} not found in database")
        return

    gcal = GoogleCalendarService(user=user)
    calendar_id = gcal.ensure_toggl_calendar()

    gcal_data = db_entry.get_gcal_data()

    # Find existing event by iCalUID
    current_event = gcal.find_event_by_ical_uid(
        calendar_id=calendar_id,
        ical_uid=db_entry.gcal_event_id,
    )

    if current_event:
        gcal.update_event(
            calendar_id=calendar_id,
            event_id=current_event["id"],
            summary=gcal_data["summary"],
            start=gcal_data["start"],
            end=gcal_data["end"],
            description=gcal_data["description"],
            color_id=gcal_data["color_id"],
        )
    else:
        # Event doesn't exist, create it
        logger.info(f"Event {entry_id} not found in calendar, creating")
        gcal.create_event(
            calendar_id=calendar_id,
            **gcal_data,
        )

    db_entry.synced = True
    db_entry.save(update_fields=["synced"])

    logger.info(
        f"Updated calendar event for entry {entry_id} (user: {user.username})"
    )


def _handle_deleted(time_entry: dict, user: User):
    """Handle a time entry deletion."""
    entry_id = time_entry.get("id")
    if not entry_id:
        logger.warning("Time entry missing ID")
        return

    try:
        db_entry = TogglTimeEntry.objects.get(user=user, toggl_id=entry_id)
    except TogglTimeEntry.DoesNotExist:
        logger.debug(f"Entry {entry_id} not found in synced entries")
        return

    calendar_id = user.credentials.google_calendar_id
    if not calendar_id:
        logger.debug(f"No calendar configured for user {user.username}, skipping delete")
        return

    gcal = GoogleCalendarService(user=user)
    event = gcal.find_event_by_ical_uid(
        calendar_id=calendar_id,
        ical_uid=db_entry.gcal_event_id,
    )
    if event:
        try:
            gcal.delete_event(calendar_id, event["id"])
            logger.info(
                f"Deleted calendar event for entry {entry_id} (user: {user.username})"
            )
        except Exception as e:
            logger.warning(f"Failed to delete calendar event: {e}")
    else:
        logger.debug(f"Event {entry_id} not found in calendar, already deleted")

    logger.info(f"Entry {entry_id} marked for deletion (kept in database)")


def sync_toggl_metadata_for_user(request, user):
    """Sync all metadata from Toggl API for a specific user."""
    creds = user.credentials
    if not creds.toggl_api_token:
        messages.error(
            request, f"Toggl API token not configured for {user.username}"
        )
        return

    try:
        toggl = TogglService(creds.toggl_api_token)

        # Sync organizations
        orgs = toggl.get_organizations()
        org_count = 0
        for org in orgs:
            TogglOrganization.objects.update_or_create(
                user=user,
                toggl_id=org["id"],
                defaults={"name": org["name"]},
            )
            org_count += 1

        # Sync workspaces
        workspaces = toggl.get_workspaces()
        ws_count = 0
        for ws in workspaces:
            # Look up organization object if organization_id is provided
            org = None
            if ws.get("organization_id"):
                org = TogglOrganization.objects.filter(
                    user=user, toggl_id=ws["organization_id"]
                ).first()

            workspace, created = TogglWorkspace.objects.update_or_create(
                user=user,
                toggl_id=ws["id"],
                defaults={
                    "name": ws["name"],
                    "organization": org,
                },
            )
            # Generate webhook token for new workspaces
            if not workspace.webhook_token:
                workspace.webhook_token = secrets.token_urlsafe(32)
                workspace.save(update_fields=["webhook_token"])
            ws_count += 1

        # Sync projects, tags, and webhooks for each workspace
        proj_count = 0
        tag_count = 0
        webhook_count = 0
        webhook_domain = settings.WEBHOOK_DOMAIN

        for ws in TogglWorkspace.objects.filter(user=user):
            try:
                projects = toggl.get_projects(ws.toggl_id)
                for project in projects:
                    TogglProject.objects.update_or_create(
                        user=user,
                        toggl_id=project["id"],
                        defaults={
                            "workspace": ws,
                            "name": project["name"],
                            "color": project.get("color"),
                            "active": project.get("active", True),
                        },
                    )
                    proj_count += 1
            except TogglAPIError:
                pass

            try:
                tags = toggl.get_tags(ws.toggl_id)
                if tags:
                    for tag in tags:
                        TogglTag.objects.update_or_create(
                            user=user,
                            toggl_id=tag["id"],
                            defaults={
                                "workspace": ws,
                                "name": tag["name"],
                            },
                        )
                        tag_count += 1
            except TogglAPIError:
                pass

            # Sync existing webhooks for this workspace
            try:
                webhooks = toggl.list_webhooks(ws.toggl_id)
                if webhooks:
                    for webhook in webhooks:
                        callback_url = webhook.get("url_callback", "")
                        # Check if this webhook points to our domain
                        if webhook_domain and webhook_domain in callback_url:
                            # Extract token from URL: .../webhook/toggl/<token>/
                            import re

                            match = re.search(
                                r"/webhook/toggl/([^/]+)/?", callback_url
                            )
                            if match:
                                token = match.group(1)
                                ws.webhook_token = token
                                ws.webhook_subscription_id = webhook.get(
                                    "subscription_id"
                                )
                                ws.webhook_secret = webhook.get("secret")
                                ws.webhook_enabled = webhook.get(
                                    "enabled", False
                                )
                                ws.save()
                                webhook_count += 1
                                logger.info(
                                    f"Found existing webhook for workspace {ws.name}: "
                                    f"subscription_id={ws.webhook_subscription_id}"
                                )
            except TogglAPIError as e:
                logger.debug(
                    f"Could not fetch webhooks for workspace {ws.toggl_id}: {e}"
                )

        # Update last sync time
        creds.last_toggl_metadata_sync = timezone.now()
        creds.save(update_fields=["last_toggl_metadata_sync"])

        msg = (
            f"Synced {org_count} organizations, {ws_count} workspaces, "
            f"{proj_count} projects, {tag_count} tags"
        )
        if webhook_count:
            msg += f", {webhook_count} existing webhooks"
        msg += f" for {user.username}"
        messages.success(request, msg)

    except TogglAPIError as e:
        messages.error(request, f"Toggl API error: {e}")
    except Exception as e:
        logger.exception("Error syncing metadata")
        messages.error(request, f"Error: {e}")
