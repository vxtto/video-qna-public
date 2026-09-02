"""Per-user identity, as forwarded by Caddy's forward_auth.

In prod, Caddy sits in front of `api` and only proxies requests that
oauth2-proxy already authenticated (see infra/caddy/Caddyfile). Because
oauth2-proxy is configured with `OAUTH2_PROXY_SET_XAUTHREQUEST: "true"`
and the Caddyfile does `copy_headers X-Auth-Request-User
X-Auth-Request-Email`, every request that reaches `api` carries the
logged-in GitHub login in `X-Auth-Request-User`. `api` itself is bound to
127.0.0.1 only (docker-compose.yml) and reachable *externally* only
through Caddy, so trusting that header is safe for traffic that actually
came in over the public listener.

It is NOT safe against something else on the same box curling
127.0.0.1:8000 directly with a forged header - that's an accepted risk
for a single-tenant VPS where only people with SSH access could do that
in the first place (same trust level SSH access already implies).

Local dev never runs the "prod" compose profile (no Caddy, no
oauth2-proxy), so the header is simply absent there - DEV_USER fills in
for it. See docs/STATUS.md / PLAN.md if that story changes.
"""

from __future__ import annotations

import os

from fastapi import Request

DEV_USER = os.environ.get("DEV_USER", "dev")

# Local-only escape hatch for exercising per-user isolation (e.g. the chat
# tray) without standing up the full prod profile: set DEV_USER_HEADER=1
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
