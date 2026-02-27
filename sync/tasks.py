import logging
import re
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.utils import timezone
from django_q.models import Schedule

from .models import (
    TogglTimeEntry, TogglOrganization, TogglWorkspace, TogglProject,
    TogglTag, UserCredentials, check_unknown_entities, resolve_color,
)
from .services import GoogleCalendarService, GoogleCalendarError, TogglService, TogglAPIError

logger = logging.getLogger(__name__)


def refresh_metadata_for_workspace(user: User, workspace_id: int):
    creds = user.credentials
    if not creds.toggl_api_token:
        logger.warning(
            f"Cannot refresh metadata: no Toggl API token for user {user.username}"
        )
        return

    try:
        toggl = TogglService(creds.toggl_api_token)

        workspace = TogglWorkspace.objects.filter(
            user=user, toggl_id=workspace_id
        ).first()
        if not workspace:
            logger.warning(f"Workspace {workspace_id} not found for user {user.username}")
            return

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
    """Process a time entry: debounce, then sync to Google Calendar."""
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"User {user_id} not found")
        return

    try:
        entry = TogglTimeEntry.objects.get(user=user, toggl_id=entry_id)
    except TogglTimeEntry.DoesNotExist:
        logger.warning(f"Entry {entry_id} not found in database")
        return

    time_since_update = (timezone.now() - entry.updated_at).total_seconds()
    wait_time = getattr(settings, 'QCLUSTER_WAIT', 60)

    if time_since_update < wait_time:
        remaining = int(wait_time - time_since_update) + 1
        logger.info(
            f"Entry {entry_id} updated {time_since_update:.1f}s ago, "
            f"waiting {remaining}s more before processing (task deferred)"
        )

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

    if not user.credentials.is_connected:
        logger.warning(
            f"Skipping entry {entry_id}: Google Calendar not connected for {user.username}"
        )
        return

    logger.info(
        f"Processing entry {entry_id} (user: {user.username}, "
        f"pending_deletion: {entry.pending_deletion})"
    )

    time_entry = {
        "id": entry.toggl_id,
        "description": entry.description,
        "start": entry.start_time.isoformat(),
        "stop": entry.end_time.isoformat() if entry.end_time else None,
        "project_id": entry.project_id,
        "tag_ids": entry.tag_ids,
    }

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

        entry.synced = True
        entry.save(update_fields=["synced"])

    except Exception as e:
        logger.exception(f"Error processing entry {entry_id}: {e}")
        retry_delay = getattr(settings, 'SYNC_ERROR_RETRY_DELAY', 300)
        next_run = timezone.now() + timezone.timedelta(seconds=retry_delay)
        Schedule.objects.update_or_create(
            name=f"retry_entry_{entry_id}",
            defaults={
                "func": "sync.tasks.process_time_entry_event",
                "args": f"({user_id}, {entry_id})",
                "schedule_type": Schedule.ONCE,
                "next_run": next_run,
            }
        )
        logger.info(
            f"Scheduled retry for entry {entry_id} in {retry_delay}s (at {next_run})"
        )


def _handle_created(time_entry: dict, user: User):
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

    color_id = resolve_color(user, time_entry)
    gcal_data = db_entry.get_gcal_data(color_id=color_id)
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
    entry_id = time_entry.get("id")
    if not entry_id:
        logger.warning("Time entry missing ID")
        return

    try:
        db_entry = TogglTimeEntry.objects.get(user=user, toggl_id=entry_id)
    except TogglTimeEntry.DoesNotExist:
        logger.error(f"Entry {entry_id} not found in database")
        return

    if not db_entry.synced:
        logger.debug(f"Entry {entry_id} not yet synced, treating as create")
        _handle_created(time_entry, user)
        return

    gcal = GoogleCalendarService(user=user)
    calendar_id = gcal.ensure_toggl_calendar()

    color_id = resolve_color(user, time_entry)
    gcal_data = db_entry.get_gcal_data(color_id=color_id)

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
    entry_id = time_entry.get("id")
    if not entry_id:
        logger.warning("Time entry missing ID")
        return

    try:
        db_entry = TogglTimeEntry.objects.get(user=user, toggl_id=entry_id)
    except TogglTimeEntry.DoesNotExist:
        logger.debug(f"Entry {entry_id} not found in synced entries")
        return

    if not user.credentials.is_connected:
        logger.debug(f"Google Calendar not connected for {user.username}, skipping delete")
        return

    gcal = GoogleCalendarService(user=user)
    calendar_id = gcal.ensure_toggl_calendar()

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
        except GoogleCalendarError as e:
            logger.error(
                f"Failed to delete calendar event for entry {entry_id}: {e}"
            )
        except Exception as e:
            logger.exception(
                f"Unexpected error deleting calendar event for entry {entry_id}: {e}"
            )
            raise
    else:
        logger.debug(f"Event {entry_id} not found in calendar, already deleted")

    logger.info(f"Entry {entry_id} marked for deletion (kept in database)")


def apply_color_to_entry(entry_id: int, color_id: str):
    try:
        entry = TogglTimeEntry.objects.get(id=entry_id)
    except TogglTimeEntry.DoesNotExist:
        logger.error(f"Entry not found: {entry_id}")
        return

    if not entry.synced:
        logger.debug(f"Skipping entry {entry.toggl_id}: not synced to Google")
        return

    user = entry.user
    if not user.credentials.is_connected:
        logger.debug(f"Google Calendar not connected for {user.username}")
        return

    gcal = GoogleCalendarService(user=user)
    calendar_id = gcal.ensure_toggl_calendar()

    try:
        event = gcal.find_event_by_ical_uid(
            calendar_id=calendar_id,
            ical_uid=entry.gcal_event_id,
        )

        if not event:
            logger.warning(
                f"Event {entry.gcal_event_id} not found, marking as not synced"
            )
            entry.synced = False
            entry.save(update_fields=["synced"])
            return

        gcal.update_event(
            calendar_id=calendar_id,
            event_id=event["id"],
            color_id=color_id,
        )
        logger.info(f"Applied color {color_id} to entry {entry.toggl_id}")

    except GoogleCalendarError as e:
        logger.error(f"Failed to apply color to entry {entry.toggl_id}: {e}")
    except Exception as e:
        logger.exception(f"Unexpected error applying color to entry {entry.toggl_id}")
        raise


def sync_toggl_metadata_for_user(request, user):
    creds = user.credentials
    if not creds.toggl_api_token:
        messages.error(
            request, f"Toggl API token not configured for {user.username}"
        )
        return

    try:
        toggl = TogglService(creds.toggl_api_token)

        orgs = toggl.get_organizations()
        org_count = 0
        for org in orgs:
            TogglOrganization.objects.update_or_create(
                user=user,
                toggl_id=org["id"],
                defaults={"name": org["name"]},
            )
            org_count += 1

        workspaces = toggl.get_workspaces()
        ws_count = 0
        for ws in workspaces:
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
            if not workspace.webhook_token:
                workspace.webhook_token = secrets.token_urlsafe(32)
                workspace.save(update_fields=["webhook_token"])
            ws_count += 1

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
            except TogglAPIError as e:
                logger.warning(
                    f"Failed to sync projects for workspace {ws.toggl_id} "
                    f"(user: {user.username}): {e}"
                )

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
            except TogglAPIError as e:
                logger.warning(
                    f"Failed to sync tags for workspace {ws.toggl_id} "
                    f"(user: {user.username}): {e}"
                )

            try:
                webhooks = toggl.list_webhooks(ws.toggl_id)
                if webhooks:
                    for webhook in webhooks:
                        callback_url = webhook.get("url_callback", "")
                        if webhook_domain and webhook_domain in callback_url:
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


def process_unsynced_entries():
    """Catch-up task: find unsynced entries and queue them. Self-rescheduling."""
    try:
        wait_time = getattr(settings, 'QCLUSTER_WAIT', 60)
        cutoff = timezone.now() - timezone.timedelta(seconds=wait_time)

        unsynced = TogglTimeEntry.objects.filter(
            synced=False,
            updated_at__lt=cutoff,
        ).select_related('user')

        count = unsynced.count()
        if count == 0:
            return

        logger.info(f"Catch-up: found {count} unsynced entries")

        queued = 0
        for entry in unsynced[:50]:
            try:
                if not entry.user.credentials.is_connected:
                    logger.debug(
                        f"Skipping entry {entry.toggl_id}: "
                        f"Google Calendar not connected for {entry.user.username}"
                    )
                    continue
            except UserCredentials.DoesNotExist:
                continue

            Schedule.objects.update_or_create(
                name=f"catchup_entry_{entry.toggl_id}",
                defaults={
                    "func": "sync.tasks.process_time_entry_event",
                    "args": f"({entry.user_id}, {entry.toggl_id})",
                    "schedule_type": Schedule.ONCE,
                    "next_run": timezone.now(),
                }
            )
            queued += 1

        logger.info(f"Catch-up: queued {queued} entries for processing")
    finally:
        _reschedule_task("process_unsynced_entries", settings.SYNC_CATCHUP_INTERVAL)


def validate_synced_events():
    """Validate synced entries exist in Google Calendar. Self-rescheduling."""
    try:
        synced_entries = TogglTimeEntry.objects.filter(
            synced=True,
            pending_deletion=False,
        ).select_related('user')

        if not synced_entries.exists():
            logger.debug("No synced entries to validate")
            return

        entries_by_user = {}
        for entry in synced_entries:
            entries_by_user.setdefault(entry.user_id, []).append(entry)

        total_checked = 0
        total_discrepancies = 0

        for user_id, entries in entries_by_user.items():
            user = entries[0].user

            try:
                if not user.credentials.is_connected:
                    continue
            except Exception:
                continue

            try:
                gcal = GoogleCalendarService(user=user)
                calendar_id = gcal.ensure_toggl_calendar()
            except GoogleCalendarError as e:
                logger.warning(f"Cannot validate events for {user.username}: {e}")
                continue
            except Exception as e:
                logger.exception(f"Unexpected error validating events for {user.username}: {e}")
                continue

            for entry in entries[:20]:
                total_checked += 1
                try:
                    event = gcal.find_event_by_ical_uid(
                        calendar_id=calendar_id,
                        ical_uid=entry.gcal_event_id,
                    )

                    if not event:
                        logger.warning(
                            f"Validation: entry {entry.toggl_id} marked synced but "
                            f"event not found in Google Calendar, marking unsynced"
                        )
                        entry.synced = False
                        entry.save(update_fields=["synced"])
                        total_discrepancies += 1
                        continue

                    expected_summary = entry.description or "(No description)"
                    actual_summary = event.get("summary", "")
                    if expected_summary != actual_summary:
                        logger.warning(
                            f"Validation: entry {entry.toggl_id} summary mismatch: "
                            f"expected={expected_summary!r}, actual={actual_summary!r}"
                        )
                        entry.synced = False
                        entry.save(update_fields=["synced"])
                        total_discrepancies += 1

                except GoogleCalendarError as e:
                    logger.warning(f"Validation: could not check entry {entry.toggl_id}: {e}")

        logger.info(
            f"Validation complete: checked {total_checked}, "
            f"found {total_discrepancies} discrepancies"
        )
    finally:
        _reschedule_task("validate_synced_events", settings.SYNC_VALIDATE_INTERVAL)


def _reschedule_task(schedule_name: str, interval_seconds: int):
    Schedule.objects.update_or_create(
        name=schedule_name,
        defaults={
            "func": f"sync.tasks.{schedule_name}",
            "schedule_type": Schedule.ONCE,
            "next_run": timezone.now() + timezone.timedelta(seconds=interval_seconds),
        }
    )


def ensure_periodic_schedules():
    """Register periodic schedules in django-q. Called on app startup."""
    catchup_seconds = getattr(settings, 'SYNC_CATCHUP_INTERVAL', 10)
    validate_seconds = getattr(settings, 'SYNC_VALIDATE_INTERVAL', 20)

    Schedule.objects.update_or_create(
        name="process_unsynced_entries",
        defaults={
            "func": "sync.tasks.process_unsynced_entries",
            "schedule_type": Schedule.ONCE,
            "next_run": timezone.now() + timezone.timedelta(seconds=catchup_seconds),
        },
    )

    Schedule.objects.update_or_create(
        name="validate_synced_events",
        defaults={
            "func": "sync.tasks.validate_synced_events",
            "schedule_type": Schedule.ONCE,
            "next_run": timezone.now() + timezone.timedelta(seconds=validate_seconds),
        },
    )

    logger.info(
        f"Periodic schedules ensured: catch-up every {catchup_seconds}s, "
        f"validation every {validate_seconds}s"
    )
