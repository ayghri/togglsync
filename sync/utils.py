from django.conf import settings

from datetime import datetime
import hashlib
import hmac

from django.db import models


class UserScopedModel(models.Model):

    class Meta:
        abstract = True

    @classmethod
    def get_for_user(cls, user):
        """Get queryset filtered to a specific user."""
        return cls.objects.filter(user=user)


def get_google_credentials():
    config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
        }
    }

    return config, settings.GOOGLE_CALENDAR_SCOPES, settings.GOOGLE_REDIRECT_URI



def parse_datetime(dt_str: str | None) -> datetime | None:
    """Parse ISO datetime string to datetime object."""
    if not dt_str:
        return None
    # Handle various ISO formats
    dt_str = dt_str.replace("Z", "+00:00")
    return datetime.fromisoformat(dt_str)


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify the webhook signature from Toggl.

    Toggl sends X-Webhook-Signature-256 header in format: sha256={hash}
    where hash is HMAC-SHA256 of the full request body using the webhook secret.

    See: https://engineering.toggl.com/docs/webhooks_start/validating_received_events/
    """

    # Toggl signature format: sha256={hash}
    if signature.startswith("sha256="):
        signature = signature[7:]  # Remove 'sha256=' prefix

    expected = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected)

