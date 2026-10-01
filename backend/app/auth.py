"""Per-user identity, as forwarded by an authenticating reverse proxy.

The app has no login of its own. When deployed, it sits behind
oauth2-proxy (GitHub OAuth), which sets `X-Auth-Request-User` on every
request it lets through; `api` is bound to 127.0.0.1, so that header can
only arrive via the proxy. Locally there is no proxy and no header, so
DEV_USER fills in.
"""

from __future__ import annotations

import os

from fastapi import Request

DEV_USER = os.environ.get("DEV_USER", "dev")

# Local-only escape hatch for exercising per-user isolation (e.g. the chat
# tray) without a reverse proxy: set DEV_ALLOW_USER_HEADER=1
# and pass X-Auth-Request-User yourself. Never honored when the real
# header is present, so it's a no-op in prod even if the env var leaked
# into that environment by mistake.
_ALLOW_DEV_HEADER_OVERRIDE = os.environ.get("DEV_ALLOW_USER_HEADER") == "1"


def get_current_user(request: Request) -> str:
    header_user = request.headers.get("x-auth-request-user")
    if header_user:
        return header_user

    if _ALLOW_DEV_HEADER_OVERRIDE:
        override = request.headers.get("x-auth-request-user-dev")
        if override:
            return override

    email = request.headers.get("x-auth-request-email")
    if email:
        return email

    return DEV_USER
