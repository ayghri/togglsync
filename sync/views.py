import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_q.tasks import async_task
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from .models import UserCredentials, TogglWorkspace, TogglTimeEntry, TogglProject, TogglTag
from .services import GoogleCalendarService
from .tasks import sync_toggl_metadata_for_user
from .utils import get_google_credentials, verify_signature, parse_datetime

logger = logging.getLogger(__name__)


def home_page(request):
    context = {"user": request.user}

    if request.user.is_authenticated:
        creds = request.user.credentials
        context["toggl_config"] = creds
        context["google_config"] = creds.is_connected
        context["google_calendar_id"] = creds.google_calendar_id
        if creds.gauth_credentials_json:
            try:
                cred_data = json.loads(creds.gauth_credentials_json)
                google_creds = Credentials.from_authorized_user_info(cred_data)
                context["google_token_expiry"] = google_creds.expiry
                if google_creds.token:
                    token = google_creds.token
                    context["google_token_masked"] = token[:8] + "****" + token[-8:] if len(token) > 16 else "****"
            except Exception as e:
                logger.warning(
                    f"Failed to load Google credentials for display "
                    f"(user: {request.user.username}): {e}"
                )

    return render(request, "user_dashboard.html", context)


@csrf_exempt
def health_check(request):
    return JsonResponse({"status": "ok"})


def privacy_policy(request):
    return render(request, "privacy_policy.html")


def terms_of_service(request):
    return render(request, "terms_of_service.html")


@csrf_exempt
@require_POST
def toggl_webhook(request, webhook_token: str):
    workspace = TogglWorkspace.objects.filter(
        webhook_token=webhook_token
    ).first()
    if not workspace:
        logger.warning(f"Unknown webhook token: {webhook_token[:8]}...")
        return HttpResponse(status=404)

    user = workspace.user

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        logger.error("Invalid JSON in webhook payload")
        return HttpResponse(status=400)

    if workspace.webhook_secret:
        signature = request.headers.get("X-Webhook-Signature-256")
        if not verify_signature(
            request.body, signature, workspace.webhook_secret
        ):
            logger.warning(
                f"Invalid webhook signature for workspace {workspace.toggl_id}"
            )
            return HttpResponse(status=401)

    inner_payload = payload.get("payload")
    metadata = payload.get("metadata", {})

    if inner_payload == "ping":
        validation_code = payload.get("validation_code")
        logger.info(
            f"Received PING from Toggl for workspace {workspace.toggl_id}, "
            f"validation_code: {validation_code}"
        )
        if validation_code:
            return JsonResponse({"validation_code": validation_code})
        return JsonResponse({"status": "ok"})

    if not isinstance(inner_payload, dict):
        logger.warning(f"Unknown webhook format or non-dict payload: {payload}")
        return JsonResponse({"status": "ok"})

    entry = inner_payload
    event_type = metadata.get("action", "").lower()
    entry_id = entry.get("id")

    if not entry_id:
        logger.warning("Time entry missing ID")
        return JsonResponse({"status": "ok"})

    if event_type not in ("created", "updated", "deleted"):
        logger.warning(f"Unknown event type: {event_type}")
        return JsonResponse({"status": "ok"})

    description = entry.get("description", "(no description)")
    project_id = entry.get("project_id")
    tag_ids = entry.get("tag_ids", [])

    log_parts = [f"{event_type} entry {entry_id}"]
    if description:
        log_parts.append(f'"{description}"')
    if project_id:
        project = TogglProject.objects.filter(user=user, toggl_id=project_id).first()
        project_str = f"project:{project_id}"
        if project:
            project_str += f"({project.name})"
        log_parts.append(project_str)
    if tag_ids:
        tags = TogglTag.objects.filter(user=user, toggl_id__in=tag_ids)
        tag_map = {t.toggl_id: t.name for t in tags}
        tag_strs = [f"{tid}({tag_map.get(tid, '?')})" for tid in tag_ids]
        log_parts.append(f"tags:[{', '.join(tag_strs)}]")

    logger.info(f"Webhook from {user.username}: {' '.join(log_parts)}")

    start_raw = entry.get("start")
    stop_raw = entry.get("stop")
    duration_raw = entry.get("duration")
    logger.debug(
        f"Webhook payload detail: entry={entry_id} "
        f"start={start_raw} stop={stop_raw} duration={duration_raw}"
    )

    webhook_created_at = parse_datetime(payload.get("created_at"))

    if event_type == "deleted":
        TogglTimeEntry.objects.filter(user=user, toggl_id=entry_id).update(
            pending_deletion=True,
            synced=False
        )
    else:
        start = parse_datetime(entry.get("start"))
        end = parse_datetime(entry.get("stop"))
        entry_exists = TogglTimeEntry.objects.filter(user=user, toggl_id=entry_id).exists()

        defaults = {
            "description": entry.get("description", ""),
            "start_time": start,
            "end_time": end,
            "project_id": entry.get("project_id"),
            "tag_ids": entry.get("tag_ids", []),
            "synced": False,
            "pending_deletion": False,
        }

        if not entry_exists and webhook_created_at:
            defaults["created_at"] = webhook_created_at

        TogglTimeEntry.objects.update_or_create(
            user=user,
            toggl_id=entry_id,
            defaults=defaults
        )

    async_task(
        "sync.tasks.process_time_entry_event",
        user.id,
        entry_id,
        group=f"user_{user.id}_entry_{entry_id}",
    )

    return JsonResponse({"status": "ok"})


@login_required
def google_oauth_start(request):
    try:
        client_config, scopes, redirect_uri = get_google_credentials()

        flow = Flow.from_client_config(
            client_config,
            scopes=scopes,
            redirect_uri=redirect_uri,
        )

        auth_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
        )

        request.session["oauth_state"] = state

        logger.info(
            f"Starting Google OAuth flow for user {request.user.username}"
        )
        return redirect(auth_url)

    except ValueError as e:
        logger.error(f"Google OAuth configuration error: {e}")
        messages.error(request, str(e))
        return redirect("sync:landing")
    except Exception as e:
        logger.exception(f"Error starting OAuth flow: {e}")
        messages.error(request, f"Error starting OAuth flow: {e}")
        return redirect("sync:landing")


@login_required
def google_oauth_callback(request):
    error = request.GET.get("error")
    if error:
        logger.warning(f"OAuth error from Google: {error}")
        messages.error(request, f"Google authorization failed: {error}")
        return redirect("sync:landing")

    state = request.GET.get("state")
    stored_state = request.session.get("oauth_state")

    if not state or state != stored_state:
        logger.warning("OAuth state mismatch")
        messages.error(request, "Invalid OAuth state. Please try again.")
        return redirect("sync:landing")

    try:
        client_config, scopes, redirect_uri = get_google_credentials()

        flow = Flow.from_client_config(
            client_config,
            scopes=scopes,
            redirect_uri=redirect_uri,
            state=state,
        )

        # Use configured redirect URI (not request URL which may differ behind proxy)
        auth_response = f"{redirect_uri}?{request.GET.urlencode()}"
        flow.fetch_token(authorization_response=auth_response)

        credentials = flow.credentials
        creds, created = UserCredentials.objects.get_or_create(
            user=request.user
        )
        creds.gauth_credentials_json = credentials.to_json()
        creds.save()

        if "oauth_state" in request.session:
            del request.session["oauth_state"]

        logger.info(
            f"Google Calendar connected for user {request.user.username}"
        )

        try:
            gcal = GoogleCalendarService(user=request.user)
            calendar_id = gcal.ensure_toggl_calendar()
            messages.success(
                request,
                f"Google Calendar connected! Toggl calendar created: {calendar_id}"
            )
        except Exception as e:
            logger.warning(f"Connected but failed to create Toggl calendar: {e}")
            messages.success(request, "Google Calendar connected successfully!")

        return redirect("sync:landing")

    except Exception as e:
        logger.exception(f"Error completing OAuth flow: {e}")
        messages.error(request, f"Error connecting Google Calendar: {e}")
        return redirect("sync:landing")


@login_required
def google_oauth_disconnect(request):
    try:
        creds = request.user.credentials
        creds.gauth_credentials_json = ""
        creds.google_calendar_id = ""
        creds.save(update_fields=["gauth_credentials_json", "google_calendar_id", "updated_at"])
        logger.info(
            f"Google Calendar disconnected for user {request.user.username}"
        )
        messages.success(request, "Google Calendar disconnected.")
    except Exception as e:
        logger.exception(f"Error disconnecting Google Calendar: {e}")
        messages.error(request, f"Error disconnecting: {e}")

    return redirect("sync:landing")


@login_required
def sync_toggl_metadata(request):
    creds = request.user.credentials
    if not creds.toggl_api_token:
        messages.error(request, "Toggl API token not configured")
        return redirect("sync:landing")

    try:
        sync_toggl_metadata_for_user(request, request.user)
    except Exception as e:
        logger.exception(f"Error syncing Toggl metadata: {e}")
        messages.error(request, f"Error syncing metadata: {e}")

    return redirect("sync:landing")


@login_required
def refresh_calendar(request):
    creds = request.user.credentials
    if not creds.is_connected:
        messages.error(request, "Google Calendar not connected")
        return redirect("sync:landing")

    try:
        gcal = GoogleCalendarService(user=request.user)
        creds.google_calendar_id = ""
        creds.save(update_fields=["google_calendar_id", "updated_at"])

        calendar_id = gcal.ensure_toggl_calendar()
        messages.success(request, f"Toggl calendar refreshed: {calendar_id}")
    except Exception as e:
        logger.exception(f"Error refreshing calendar: {e}")
        messages.error(request, f"Error refreshing calendar: {e}")

    return redirect("sync:landing")
