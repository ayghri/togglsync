"""
Django settings for togglsync project.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY", "django-insecure-change-me-in-production"
)

DEBUG = os.getenv("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")
DATA_DIR = Path(os.getenv("DJANGO_DATA_DIR", BASE_DIR / "data"))

ALLOWED_HOSTS = []

for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(","):
    host = host.strip()
    if host:
        ALLOWED_HOSTS.append(host)

CSRF_TRUSTED_ORIGINS = []

WEBHOOK_DOMAIN = os.getenv("DJANGO_WEBHOOK_DOMAIN", "")
if WEBHOOK_DOMAIN:
    CSRF_TRUSTED_ORIGINS.append(f"https://{WEBHOOK_DOMAIN}")
    ALLOWED_HOSTS.append(WEBHOOK_DOMAIN)
else:
    WEBHOOK_DOMAIN = "localhost:8080"

# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_q",
    "sync",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
        "OPTIONS": {
            "timeout": 30,
        },
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"
    },
]


# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
}

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# =============================================================================

LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/admin/"


TOGGL_API_ENDPOINT = "https://api.track.toggl.com/api/v9"
TOGGL_WEBHOOK_API_ENDPOINT = "https://api.track.toggl.com/webhooks/api/v1"

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

GOOGLE_REDIRECT_URI = f"https://{WEBHOOK_DOMAIN}/oauth/google/callback/"
GOOGLE_CALENDAR_TIMEZONE = os.getenv("GOOGLE_CALENDAR_TIMEZONE", "UTC")

GOOGLE_CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.app.created",
]


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {module} {message}",
            "style": "{",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "simple": {
            "format": "{asctime} {levelname} {message}",
            "style": "{",
            "datefmt": "%H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple" if DEBUG else "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "sync": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else os.getenv("SYNC_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "django_q": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}


Q_CLUSTER = {
    "name": "togglsync",
    "workers": 1,
    "timeout": 60,
    "retry": 120,
    "orm": "default",
}

SYNC_VALIDATE_INTERVAL = int(os.getenv("SYNC_VALIDATE_INTERVAL", "10"))
