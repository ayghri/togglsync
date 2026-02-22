"""Webhook views for handling Toggl Track events."""

import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_q.tasks import async_task
from google_auth_oauthlib.flow import Flow

from .models import UserCredentials
from .models import TogglWorkspace, TogglTimeEntry
from .tasks import sync_toggl_metadata_for_user
from .utils import get_google_credentials, verify_signature, parse_datetime

logger = logging.getLogger(__name__)


def home_page(request):
    """Landing page that shows login or setup status."""
    context = {
        "user": request.user,
    }

    if request.user.is_authenticated:
        # Check setup status
        creds = request.user.credentials

        # Toggl configuration
        context["toggl_config"] = creds

        # Google Calendar configuration
        context["google_config"] = creds.is_connected
        if creds.gauth_credentials_json:
            from google.oauth2.credentials import Credentials
            try:
                cred_data = json.loads(creds.gauth_credentials_json)
                google_creds = Credentials.from_authorized_user_info(cred_data)
                context["google_token_expiry"] = google_creds.expiry
                # Mask the token for display
                if google_creds.token:
                    token = google_creds.token
                    context["google_token_masked"] = token[:8] + "****" + token[-8:] if len(token) > 16 else "****"
            except Exception:
                pass

    return render(request, "user_dashboard.html", context)


@csrf_exempt
def health_check(request):
    """Simple health check endpoint that returns 200 OK."""
    return JsonResponse({"status": "ok"})


def privacy_policy(request):
    """Privacy policy page for Google OAuth consent screen."""
    return render(request, "privacy_policy.html")


def terms_of_service(request):
    """Terms of service page for Google OAuth consent screen."""
    return render(request, "terms_of_service.html")


@csrf_exempt
@require_POST
def toggl_webhook(request, webhook_token: str):
    """
    Handle incoming webhooks from Toggl Track.

    The webhook_token in the URL identifies which user/workspace this webhook is for.

    Toggl sends:
    - PING events for validation
    - time_entry events (created, updated, deleted)
    """
    # Find workspace by webhook token (identifies user)
    workspace = TogglWorkspace.objects.filter(
        webhook_token=webhook_token
    ).first()
    if not workspace:
        logger.warning(f"Unknown webhook token: {webhook_token[:8]}...")
        return HttpResponse(status=404)

    user = workspace.user

    # Parse payload
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        logger.error("Invalid JSON in webhook payload")
        return HttpResponse(status=400)

    # Verify signature using workspace-specific secret
    # See: https://engineering.toggl.com/docs/webhooks_start/validating_received_events/
    if workspace.webhook_secret:
        signature = request.headers.get("X-Webhook-Signature-256")
        if not verify_signature(
            request.body, signature, workspace.webhook_secret
        ):
            logger.warning(
                f"Invalid webhook signature for workspace {workspace.toggl_id}"
            )
            return HttpResponse(status=401)

    # Extract event info from Toggl webhook structure
    # See: https://engineering.toggl.com/docs/webhooks_start/
    inner_payload = payload.get("payload")
    metadata = payload.get("metadata", {})
    # Event type is in metadata.action (created, updated, deleted)

    # Handle PING validation
    # Toggl sends a validation_code that must be returned to validate the subscription
    # The payload field will be the string "ping" for PING events
    if inner_payload == "ping":
        validation_code = payload.get("validation_code")
        logger.info(
            f"Received PING from Toggl for workspace {workspace.toggl_id}, "
            f"validation_code: {validation_code}"
        )
        # Toggl requires EXACTLY this format for synchronous validation
        if validation_code:
            return JsonResponse({"validation_code": validation_code})
        return JsonResponse({"status": "ok"})

    # Handle time entry events
    # At this point, inner_payload should be a dict with time entry data
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

    # Log webhook with relevant details only
    description = entry.get("description", "(no description)")
    project_id = entry.get("project_id")
    tag_ids = entry.get("tag_ids", [])

    log_parts = [f"{event_type} entry {entry_id}"]
    if description:
        log_parts.append(f'"{description}"')
    if project_id:
        # Look up project name
        from sync.models import TogglProject
        project = TogglProject.objects.filter(user=user, toggl_id=project_id).first()
        project_str = f"project:{project_id}"
        if project:
            project_str += f"({project.name})"
        log_parts.append(project_str)
    if tag_ids:
        # Look up tag names
        from sync.models import TogglTag
        tags = TogglTag.objects.filter(user=user, toggl_id__in=tag_ids)
        tag_map = {t.toggl_id: t.name for t in tags}
        tag_strs = [f"{tid}({tag_map.get(tid, '?')})" for tid in tag_ids]
        log_parts.append(f"tags:[{', '.join(tag_strs)}]")

    logger.info(f"Webhook from {user.username}: {' '.join(log_parts)}")

    # Store/update the time entry immediately with synced=False
    # Extract webhook creation time
    webhook_created_at = parse_datetime(payload.get("created_at"))

    if event_type == "deleted":
        # Mark for deletion
        TogglTimeEntry.objects.filter(user=user, toggl_id=entry_id).update(
            pending_deletion=True,
            synced=False
        )
    else:
        # Create or update the entry
        start = parse_datetime(entry.get("start"))
        end = parse_datetime(entry.get("stop"))

        # Check if entry exists
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

        # Only set created_at for new entries
        if not entry_exists and webhook_created_at:
            defaults["created_at"] = webhook_created_at

        TogglTimeEntry.objects.update_or_create(
            user=user,
            toggl_id=entry_id,
            defaults=defaults
        )

    # Queue task - it will check if enough time has passed and reschedule if needed
    # Group by user_id and entry_id to track related tasks
    async_task(
        "sync.tasks.process_time_entry_event",
        user.id,
        entry_id,
        group=f"user_{user.id}_entry_{entry_id}",
    )

    return JsonResponse({"status": "ok"})


@login_required
def google_oauth_start(request):
    """Redirect user to Google for authorization."""
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

        # Store state in session for verification
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
    """Handle callback from Google OAuth, store credentials in DB."""
    # Check for errors from Google
    error = request.GET.get("error")
    if error:
        logger.warning(f"OAuth error from Google: {error}")
        messages.error(request, f"Google authorization failed: {error}")
        return redirect("sync:landing")

    # Verify state parameter
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

        # Build the authorization response URL using the configured redirect URI
        # (not the request URL which may have wrong scheme behind a proxy)
        auth_response = f"{redirect_uri}?{request.GET.urlencode()}"

        # Exchange authorization code for credentials
        flow.fetch_token(authorization_response=auth_response)

        credentials = flow.credentials

        # Store credentials in database
        creds, created = UserCredentials.objects.get_or_create(
            user=request.user
        )
        creds.gauth_credentials_json = credentials.to_json()
        creds.save()

        # Clear OAuth state from session
        if "oauth_state" in request.session:
            del request.session["oauth_state"]

        logger.info(
            f"Google Calendar connected for user {request.user.username}"
        )
        messages.success(request, "Google Calendar connected successfully!")

        return redirect("sync:landing")

    except Exception as e:
        logger.exception(f"Error completing OAuth flow: {e}")
        messages.error(request, f"Error connecting Google Calendar: {e}")
        return redirect("sync:landing")


@login_required
def google_oauth_disconnect(request):
    """Disconnect Google Calendar (clear OAuth tokens)."""
    try:
        creds = request.user.credentials
        # Clear the OAuth tokens
        creds.gauth_credentials_json = ""
        creds.save()
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
    """Sync metadata from Toggl API."""
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
