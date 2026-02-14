"""Background tasks for processing webhook events."""

import logging
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.utils import timezone
from django_q.models import Schedule

from .models import TogglTimeEntry
from .models import GoogleCal, TogglOrganization
from .models import TogglWorkspace, TogglProject
from .models import TogglTag, check_unknown_entities
from .services import CalendarResolver, GoogleCalendarService
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
            # Check if this is a new entry or an update
            if entry.calendar_id:
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

    # Resolve which calendar to use
    resolver = CalendarResolver(user)
    resolved = resolver.resolve(time_entry)

    if not resolved:
        logger.warning(f"No calendar found for entry {entry_id}, skipping")
        return

    calendar = resolved.calendar

    # Prepare Google Calendar event data
    gcal_data = db_entry.get_gcal_data(color_id=resolved.color_id)
    is_running = not db_entry.end_time

    # Create Google Calendar event
    gcal = GoogleCalendarService(user=user)
    event = gcal.create_event(
        calendar_id=calendar.calendar_id,
        **gcal_data,
    )

    # Update the entry with calendar reference and mark as synced
    db_entry.calendar = calendar
    db_entry.synced = True
    db_entry.save(update_fields=["calendar", "synced"])

    logger.info(
        f"Created calendar event for entry {entry_id} (user: {user.username}, running: {is_running})"
    )


def _handle_updated(time_entry: dict, user: User):
    """Handle a time entry update."""
    entry_id = time_entry.get("id")
    if not entry_id:
        logger.warning("Time entry missing ID")
        return

    # Get existing entry
    try:
        db_entry = TogglTimeEntry.objects.get(user=user, toggl_id=entry_id)
    except TogglTimeEntry.DoesNotExist:
        logger.error(f"Entry {entry_id} not found in database")
        return

    # If no calendar assigned yet, treat as create
    if not db_entry.calendar:
        logger.debug(f"Entry {entry_id} has no calendar, treating as create")
        _handle_created(time_entry, user)
        return

    # If entry is now running (stop time removed), delete it
    if not db_entry.end_time:
        logger.debug(
            f"Entry {entry_id} stop time removed, deleting calendar event"
        )
        _handle_deleted(time_entry, user)
        return

    # Re-resolve calendar (tags/project may have changed)
    resolver = CalendarResolver(user)
    resolved = resolver.resolve(time_entry)

    if not resolved:
        logger.warning(f"No calendar found for entry {entry_id}, deleting")
        _handle_deleted(time_entry, user)
        return

    new_calendar = resolved.calendar

    # Prepare Google Calendar event data
    gcal_data = db_entry.get_gcal_data(color_id=resolved.color_id)

    gcal = GoogleCalendarService(user=user)

    # Find the current event by iCalUID to get the real Google event ID
    current_event = gcal.find_event_by_ical_uid(
        calendar_id=db_entry.calendar.calendar_id,
        ical_uid=db_entry.gcal_event_id
    )

    # Check if calendar changed
    if db_entry.calendar_id != new_calendar.id:
        logger.info(
            f"Entry {entry_id} calendar changed from "
            f'"{db_entry.calendar.name}" to "{new_calendar.name}"'
        )
        # Delete from old calendar if it exists
        if current_event:
            try:
                gcal.delete_event(
                    db_entry.calendar.calendar_id,
                    current_event['id'],
                )
            except Exception as e:
                logger.warning(f"Failed to delete old event: {e}")
        else:
            logger.debug(f"Event {entry_id} not found in old calendar, skipping delete")

        # Create in new calendar
        gcal.create_event(
            calendar_id=new_calendar.calendar_id,
            **gcal_data,
        )

        db_entry.calendar = new_calendar
        db_entry.save(update_fields=["calendar"])
    else:
        # Update existing event (or create if it doesn't exist)
        if current_event:
            google_event_id = current_event['id']
            gcal.update_event(
                calendar_id=db_entry.calendar.calendar_id,
                event_id=google_event_id,
                summary=gcal_data["summary"],
                start=gcal_data["start"],
                end=gcal_data["end"],
                description=gcal_data["description"],
                color_id=gcal_data["color_id"],
            )
        else:
            # Event doesn't exist, create it
            logger.info(
                f"Event {entry_id} not found in calendar, creating"
            )
            gcal.create_event(
                calendar_id=db_entry.calendar.calendar_id,
                **gcal_data,
            )

    # Mark as synced
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

    # Find existing synced entry
    try:
        db_entry = TogglTimeEntry.objects.get(user=user, toggl_id=entry_id)
    except TogglTimeEntry.DoesNotExist:
        logger.debug(f"Entry {entry_id} not found in synced entries")
        return

    # Delete from Google Calendar if it has a calendar assigned
    if db_entry.calendar:
        gcal = GoogleCalendarService(user=user)
        # Find event by iCalUID to get the real Google Calendar event ID
        event = gcal.find_event_by_ical_uid(
            calendar_id=db_entry.calendar.calendar_id,
            ical_uid=db_entry.gcal_event_id
        )
        if event:
            try:
                gcal.delete_event(
                    db_entry.calendar.calendar_id,
                    event['id'],
                )
                logger.info(
                    f"Deleted calendar event for entry {entry_id} (user: {user.username})"
                )
            except Exception as e:
                logger.warning(f"Failed to delete calendar event: {e}")
        else:
            logger.debug(f"Event {entry_id} not found in calendar, already deleted")

    # Keep the entry record marked as pending_deletion
    # User can manually delete it later if needed
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


def import_google_calendars_for_user(request, user):
    """Import calendars from Google Calendar API for a user."""
    try:
        gcal = GoogleCalendarService(user=user)
        google_calendars = gcal.get_all_calendars()

        # Track which calendar IDs exist in Google
        google_calendar_ids = set()

        imported = 0
        updated = 0
        skipped = 0
        for gcal_data in google_calendars:
            calendar_id = gcal_data["id"]
            name = gcal_data.get("summary", calendar_id)
            access_role = gcal_data.get("accessRole", "")

            # Only import calendars with write access (owner or writer)
            if access_role not in ("owner", "writer"):
                logger.debug(f"Skipping read-only calendar: {name} (access: {access_role})")
                skipped += 1
                continue

            google_calendar_ids.add(calendar_id)

            _, created = GoogleCal.objects.update_or_create(
                user=user,
                calendar_id=calendar_id,
                defaults={"name": name},
            )
            if created:
                imported += 1
            else:
                updated += 1

        # Remove calendars that no longer exist in Google
        # will automatically remove associated mappings
        removed = GoogleCal.objects.filter(user=user).exclude(
            calendar_id__in=google_calendar_ids
        ).delete()[0]

        # Set default if none exists
        if not GoogleCal.objects.filter(user=user, is_default=True).exists():
            # Try to find primary calendar
            primary = GoogleCal.objects.filter(
                user=user, calendar_id=user.email
            ).first()
            if not primary:
                primary = GoogleCal.objects.filter(user=user).first()
            if primary:
                primary.is_default = True
                primary.save()

        msg = f"Imported {imported} new, updated {updated} existing"
        if removed:
            msg += f", removed {removed} deleted (including mappings)"
        if skipped:
            msg += f", skipped {skipped} read-only"
        messages.success(request, msg)

    except GoogleCalendarError as e:
        messages.error(request, f"Google Calendar error: {e}")
    except Exception as e:
        logger.exception("Error importing calendars")
        messages.error(request, f"Error: {e}")


def apply_mapping_to_entry(entry_id: int, calendar_id: int, color_id: str):
    """
    Apply a calendar mapping to a time entry by updating the Google Calendar event.

    Args:
        entry_id: TogglTimeEntry database ID
        calendar_id: GoogleCal database ID to move/assign the event to
        color_id: Google Calendar color ID (1-11) to apply to the event
    """
    try:
        entry = TogglTimeEntry.objects.select_related('calendar').get(id=entry_id)
        new_calendar = GoogleCal.objects.get(id=calendar_id)
    except (TogglTimeEntry.DoesNotExist, GoogleCal.DoesNotExist) as e:
        logger.error(f"Entry or calendar not found: {e}")
        return

    # Skip if no calendar assigned (event doesn't exist in Google Calendar)
    if not entry.calendar:
        logger.debug(f"Skipping entry {entry.toggl_id}: no calendar assigned (not synced to Google)")
        return

    user = entry.user
    gcal = GoogleCalendarService(user=user)

    try:
        # If entry has no calendar, create the event
        if not entry.calendar:
            logger.info(
                f"Entry {entry.toggl_id} has no calendar, creating event in '{new_calendar.name}'"
            )
            # Prepare event data
            gcal_data = entry.get_gcal_data(color_id=color_id)
            # Create the event
            event = gcal.create_event(
                calendar_id=new_calendar.calendar_id,
                **gcal_data
            )
            # Update entry with calendar and mark as synced
            entry.calendar = new_calendar
            entry.synced = True
            entry.save(update_fields=['calendar', 'synced'])
            logger.info(f"Created event for entry {entry.toggl_id} in calendar '{new_calendar.name}'")
            return

        # Find the event by iCalUID (not by event_id)
        # The iCalUID is stable, but the Google Calendar event ID may differ
        current_event = gcal.find_event_by_ical_uid(
            calendar_id=entry.calendar.calendar_id,
            ical_uid=entry.gcal_event_id
        )

        if not current_event:
            logger.warning(
                f"Event with iCalUID {entry.gcal_event_id} not found in calendar {entry.calendar.name}, "
                f"marking entry {entry.toggl_id} as not synced"
            )
            entry.synced = False
            entry.save(update_fields=['synced'])
            return

        # Get the actual Google Calendar event ID
        google_event_id = current_event['id']

        # Check if we need to move the event to a different calendar
        if entry.calendar.id != new_calendar.id:
            logger.info(
                f"Moving entry {entry.toggl_id} from calendar '{entry.calendar.name}' "
                f"to '{new_calendar.name}'"
            )
            moved_event = gcal.move_event(
                source_calendar_id=entry.calendar.calendar_id,
                destination_calendar_id=new_calendar.calendar_id,
                event_id=google_event_id
            )
            # Update the event ID after move (it may change)
            google_event_id = moved_event['id']
            # Update the calendar reference
            entry.calendar = new_calendar
            entry.save(update_fields=['calendar'])

        # Update the event color
        logger.info(f"Updating color for entry {entry.toggl_id} to color ID {color_id}")
        gcal.update_event(
            calendar_id=new_calendar.calendar_id,
            event_id=google_event_id,
            color_id=color_id
        )

    except GoogleCalendarError as e:
        if "404" in str(e) or "Not Found" in str(e):
            logger.warning(
                f"Event not found for entry {entry.toggl_id}, marking as not synced"
            )
            entry.synced = False
            entry.save(update_fields=['synced'])
        else:
            logger.error(f"Failed to apply mapping to entry {entry.toggl_id}: {e}")
    except Exception as e:
        logger.exception(f"Unexpected error applying mapping to entry {entry.toggl_id}")
        raise
