"""Superset configuration for frontflow's embedded dashboards.

Mounted into the Superset containers at /app/pythonpath/superset_config.py.

The settings that matter for embedding are grouped and commented below —
each one has a distinct, and distinctly confusing, failure mode when wrong.
"""

import os
from datetime import timedelta

# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------
SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]

SQLALCHEMY_DATABASE_URI = (
    f"postgresql+psycopg2://{os.environ['DATABASE_USER']}:"
    f"{os.environ['DATABASE_PASSWORD']}@{os.environ['DATABASE_HOST']}:"
    f"{os.environ['DATABASE_PORT']}/{os.environ['DATABASE_DB']}"
)

# --------------------------------------------------------------------------
# Embedding
# --------------------------------------------------------------------------
# Without this the "Embed dashboard" menu item does not appear at all, and
# the guest token endpoint 404s.
FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": True,
    "DASHBOARD_NATIVE_FILTERS": True,
}

# Guest tokens are what the frontend authenticates the iframe with. The
# backend mints them; see backend/app/routers/superset.py.
GUEST_ROLE_NAME = "Gamma"
GUEST_TOKEN_JWT_SECRET = os.environ["GUEST_TOKEN_JWT_SECRET"]
GUEST_TOKEN_JWT_ALGO = "HS256"
GUEST_TOKEN_HEADER_NAME = "X-GuestToken"
# The embedded SDK re-mints automatically before expiry, so a short life is
# fine and is the safer default.
GUEST_TOKEN_JWT_EXP_SECONDS = 300

# --------------------------------------------------------------------------
# Cross-origin access
# --------------------------------------------------------------------------
# The frontend is a different origin from Superset, so both of the following
# are required. Symptom of missing CORS: the iframe loads but every chart
# request fails. Symptom of Talisman's default CSP: the iframe never renders
# at all and the console shows a frame-ancestors violation.
ENABLE_CORS = True
CORS_OPTIONS = {
    "supports_credentials": True,
    "allow_headers": ["*"],
    "resources": ["*"],
    "origins": [
        os.environ.get("CORS_ALLOWED_ORIGIN", "http://localhost:8000"),
    ],
}

# Local development only.
#
# For any real deployment, do NOT ship this. Set TALISMAN_ENABLED = True and
# pin frame-ancestors to the app's origin instead, e.g.:
#
#   TALISMAN_ENABLED = True
#   TALISMAN_CONFIG = {
#       "content_security_policy": {
#           "frame-ancestors": ["https://forms.example.com"],
#       },
#       "force_https": True,
#   }
TALISMAN_ENABLED = False

# --------------------------------------------------------------------------
# Session cookie, for the in-page EDIT iframe
# --------------------------------------------------------------------------
# The read-only embed uses a guest token and needs none of this. Editing does:
# it iframes Superset's own dashboard UI with the operator's real session, and
# a cross-origin iframe only receives the session cookie when SameSite is
# "None" — the Flask default of "Lax" silently withholds it, which presents as
# a login wall inside the panel.
#
# SameSite=None additionally requires Secure. Browsers treat http://localhost
# as a secure context so this works in local development; over plain HTTP on
# any other host the cookie will be dropped. Deploy behind TLS.
SESSION_COOKIE_SAMESITE = "None"
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True

# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------
# The frontend forces genuinely fresh results by moving a native filter's
# value on every submit, which changes the query cache key. So the cache is
# left on — it still helps for repeated identical queries, and it does not
# stand between a submission and the chart showing it.
#
# If you ever drop the setDataMask push and rely on the dashboard's timed
# refresh_frequency instead (see the README), DATA_CACHE_CONFIG's timeout must
# then be shorter than that interval, or the dashboard will re-query on
# schedule and still paint stale numbers.
REDIS_URL = "redis://redis:6379"

CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 60,
    "CACHE_KEY_PREFIX": "superset_",
    "CACHE_REDIS_URL": f"{REDIS_URL}/1",
}

DATA_CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 30,
    "CACHE_KEY_PREFIX": "superset_data_",
    "CACHE_REDIS_URL": f"{REDIS_URL}/2",
}

FILTER_STATE_CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 86400,
    "CACHE_KEY_PREFIX": "superset_filter_",
    "CACHE_REDIS_URL": f"{REDIS_URL}/3",
}

EXPLORE_FORM_DATA_CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 86400,
    "CACHE_KEY_PREFIX": "superset_form_",
    "CACHE_REDIS_URL": f"{REDIS_URL}/4",
}

# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------
# Allow charts to query recent data without a hard time-range floor.
SQLLAB_TIMEOUT = 60
SUPERSET_WEBSERVER_TIMEOUT = 120


# --------------------------------------------------------------------------
# Session persistence
# --------------------------------------------------------------------------
# Superset never marks its Flask session permanent, so by default the
# session cookie has no expiry: it is a browser-session cookie that dies
# the moment the browser fully quits. Signing in again on every restart is
# especially tedious here, because the dashboard panel's Edit and
# New-chart surfaces are Superset's own UI framed with that session — a
# dead cookie shows up as a login screen inside the panel.
#
# Marking the session permanent gives the cookie a real lifetime, so one
# sign-in lasts. This changes only how long a session survives. It does
# not change who may sign in, or what they may do: an unauthenticated
# visitor is still unauthenticated.
SESSION_LIFETIME_DAYS = int(os.environ.get("SUPERSET_SESSION_DAYS", "30"))
PERMANENT_SESSION_LIFETIME = timedelta(days=SESSION_LIFETIME_DAYS)
# Honoured when the sign-in form offers "remember me".
REMEMBER_COOKIE_DURATION = timedelta(days=SESSION_LIFETIME_DAYS)


def _make_sessions_permanent(app):
    """Give each session the configured lifetime instead of expiring with
    the browser. Flask only applies PERMANENT_SESSION_LIFETIME to sessions
    marked permanent, and nothing in Superset marks them."""
    from flask import session

    @app.before_request
    def _mark_permanent() -> None:
        session.permanent = True


FLASK_APP_MUTATOR = _make_sessions_permanent
