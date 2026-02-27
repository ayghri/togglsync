import hashlib
import hmac
from datetime import datetime

from django.conf import settings

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
    if not dt_str:
        return None
    dt_str = dt_str.replace("Z", "+00:00")
    return datetime.fromisoformat(dt_str)


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify Toggl webhook HMAC-SHA256 signature."""
    if signature.startswith("sha256="):
        signature = signature[7:]

    expected = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected)

