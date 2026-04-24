"""
Django settings for Pro Attend AI.

Values that differ between dev and prod (SECRET_KEY, DEBUG, ALLOWED_HOSTS,
CSRF origins, DB path) are read from environment variables. See `.env.example`
for the full list of recognised variables.

For local development, either:
  * copy `.env.example` to `.env` and source it, OR
  * export the variables in your shell.

Never commit a `.env` file with real secrets.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default=None):
    val = os.environ.get(name)
    if not val:
        return list(default or [])
    return [item.strip() for item in val.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Core security
# ---------------------------------------------------------------------------
# SECURITY WARNING: keep the secret key used in production secret!
# Generate one with:
#   python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-CHANGE-ME-before-any-real-deployment",
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = _env_bool("DJANGO_DEBUG", default=False)

ALLOWED_HOSTS = _env_list(
    "DJANGO_ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1"] if DEBUG else [],
)

CSRF_TRUSTED_ORIGINS = _env_list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])


# ---------------------------------------------------------------------------
# Application definition
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Use the AppConfig so ready() runs (kicks off background model warm-up).
    "Attendance.apps.AttendanceConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "My_Project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "My_Project.wsgi.application"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# Defaults to SQLite at `<BASE_DIR>/db.sqlite3`. Override with:
#   DJANGO_DB_PATH=/absolute/path/to/db.sqlite3
# For production, swap the `DATABASES` block for a Postgres config.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DJANGO_DB_PATH", BASE_DIR / "db.sqlite3"),
    }
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------------
# i18n / tz
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------------
# Static & media
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles_collected"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Attendance app config
# ---------------------------------------------------------------------------

# --- Face recognition ---
ATTENDANCE_SIMILARITY_THRESHOLD = float(
    os.environ.get("ATTENDANCE_SIMILARITY_THRESHOLD", 0.4)
)
ATTENDANCE_MODELS_DIR = os.path.join(BASE_DIR, "Attendance", "Models")

# --- Image processing ---
ATTENDANCE_CROP_WIDTH = 100
ATTENDANCE_CROP_HEIGHT = 100
ATTENDANCE_MIN_BRIGHTNESS = 0.6
ATTENDANCE_MAX_BRIGHTNESS = 0.9

# --- RTSP streaming ---
ATTENDANCE_STREAM_FPS = int(os.environ.get("ATTENDANCE_STREAM_FPS", 25))
ATTENDANCE_STREAM_RETRY_DELAY = 5
ATTENDANCE_STREAM_READ_ATTEMPTS = 10

# --- Uploads ---
ATTENDANCE_MAX_UPLOAD_MB = int(os.environ.get("ATTENDANCE_MAX_UPLOAD_MB", 60))

# --- Accuracy tuning ---
ATTENDANCE_MULTI_FRAME_COUNT = int(os.environ.get("ATTENDANCE_MULTI_FRAME_COUNT", 5))
ATTENDANCE_MIN_FACE_SIZE = int(os.environ.get("ATTENDANCE_MIN_FACE_SIZE", 30))


# ---------------------------------------------------------------------------
# Production hardening (applied automatically when DEBUG=False)
# ---------------------------------------------------------------------------
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = _env_bool("DJANGO_SSL_REDIRECT", default=True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", 60 * 60 * 24 * 30))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
