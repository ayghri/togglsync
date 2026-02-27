import logging
import re
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.utils import timezone

from .models import (
    TogglTimeEntry, TogglOrganization, TogglWorkspace, TogglProject,
    TogglTag, EntityColorMapping,
)
from .services import GoogleCalendarService, GoogleCalendarError, TogglService, TogglAPIError

logger = logging.getLogger(__name__)


def process_time_entry_event(user_id: int, entry_id: int):
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

    if not user.credentials.is_connected:
        logger.warning(f"Skipping entry {entry_id}: Google Calendar not connected")
        return

    logger.info(f"Processing entry {entry_id} (user: {user.username})")

    try:
        if entry.pending_deletion:
            _handle_deleted(entry)
        else:
            _sync_to_calendar(entry)

        updated = TogglTimeEntry.objects.filter(
            id=entry.id,
            updated_at=entry.updated_at,
        ).update(synced=True)

        if not updated:
            logger.info(
                f"Entry {entry_id} was modified during sync, "
                f"skipping synced=True (next task will handle it)"
            )

    except Exception as e:
        logger.exception(f"Error processing entry {entry_id}: {e}")


def _refresh_unknown_metadata(entry: TogglTimeEntry):
    """Fetch metadata from Toggl if entry references unknown projects/tags."""
    user = entry.user
    unknown_project = (
        entry.project_id
        and not TogglProject.objects.filter(user=user, toggl_id=entry.project_id).exists()
    )
    unknown_tags = (
        entry.tag_ids
        and not TogglTag.objects.filter(user=user, toggl_id__in=entry.tag_ids).count() == len(entry.tag_ids)
    )
    if not unknown_project and not unknown_tags:
        return

    creds = user.credentials
    if not creds.toggl_api_token:
        return

    toggl = TogglService(creds.toggl_api_token)
    for ws in TogglWorkspace.objects.filter(user=user):
        try:
            for project in toggl.get_projects(ws.toggl_id):
                TogglProject.objects.update_or_create(
                    user=user, toggl_id=project["id"],
                    defaults={"workspace": ws, "name": project["name"],
                              "color": project.get("color"), "active": project.get("active", True)},
                )
        except TogglAPIError:
            pass
        try:
            tags = toggl.get_tags(ws.toggl_id)
            if tags:
                for tag in tags:
                    TogglTag.objects.update_or_create(
                        user=user, toggl_id=tag["id"],
                        defaults={"workspace": ws, "name": tag["name"]},
                    )
        except TogglAPIError:
            pass


def _sync_to_calendar(entry: TogglTimeEntry):
    user = entry.user
    _refresh_unknown_metadata(entry)
    color_id = EntityColorMapping.resolve_color(user, project_id=entry.project_id, tag_ids=entry.tag_ids)
    gcal_data = entry.get_gcal_data(color_id=color_id)

    gcal = GoogleCalendarService(user=user)
    calendar_id = gcal.ensure_toggl_calendar()

    existing = gcal.find_event_by_ical_uid(
        calendar_id=calendar_id,
        ical_uid=entry.gcal_event_id,
    )

    if existing:
        gcal.update_event(
            calendar_id=calendar_id,
            event_id=existing["id"],
            summary=gcal_data["summary"],
            start=gcal_data["start"],
            end=gcal_data["end"],
            description=gcal_data["description"],
            color_id=gcal_data["color_id"],
        )
        logger.info(f"Updated calendar event for entry {entry.toggl_id}")
    else:
        gcal.create_event(calendar_id=calendar_id, **gcal_data)
        logger.info(f"Created calendar event for entry {entry.toggl_id}")


def _handle_deleted(entry: TogglTimeEntry):
    user = entry.user
    gcal = GoogleCalendarService(user=user)
    calendar_id = gcal.ensure_toggl_calendar()

    event = gcal.find_event_by_ical_uid(
        calendar_id=calendar_id,
        ical_uid=entry.gcal_event_id,
    )
    if event:
        gcal.delete_event(calendar_id, event["id"])
        logger.info(f"Deleted calendar event for entry {entry.toggl_id}")
    else:
        logger.debug(f"Event {entry.toggl_id} not found in calendar, already deleted")


def apply_color_to_entry(entry_id: int, color_id: str):
    try:
        entry = TogglTimeEntry.objects.get(id=entry_id)
    except TogglTimeEntry.DoesNotExist:
        return

    if not entry.synced:
        return

    user = entry.user
    gcal = GoogleCalendarService(user=user)
    calendar_id = gcal.ensure_toggl_calendar()

    event = gcal.find_event_by_ical_uid(
        calendar_id=calendar_id,
        ical_uid=entry.gcal_event_id,
    )
    if not event:
        entry.synced = False
        entry.save(update_fields=["synced"])
        return

    gcal.update_event(
        calendar_id=calendar_id,
        event_id=event["id"],
        color_id=color_id,
    )
    logger.info(f"Applied color {color_id} to entry {entry.toggl_id}")


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


def validate_synced_events():
    synced_entries = TogglTimeEntry.objects.filter(
        synced=True,
        pending_deletion=False,
    ).select_related('user')

    if not synced_entries.exists():
        return

    entries_by_user = {}
    for entry in synced_entries:
        entries_by_user.setdefault(entry.user_id, []).append(entry)

    total_checked = 0
    total_discrepancies = 0

    for user_id, entries in entries_by_user.items():
        user = entries[0].user

        if not user.credentials.is_connected:
            continue

        try:
            gcal = GoogleCalendarService(user=user)
            calendar_id = gcal.ensure_toggl_calendar()
        except Exception as e:
            logger.warning(f"Cannot validate events for {user.username}: {e}")
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
                        f"Validation: entry {entry.toggl_id} not found in calendar, marking unsynced"
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

    if total_discrepancies:
        logger.info(
            f"Validation: checked {total_checked}, "
            f"found {total_discrepancies} discrepancies"
        )
