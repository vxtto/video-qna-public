-- Per-user session scoping. Caddy's forward_auth now copies
-- X-Auth-Request-User (GitHub login) through to `api` on every request
-- (infra/caddy/Caddyfile, since #5/#6) - this stamps that identity onto
-- sessions so /api/sessions can be scoped per user instead of listing
-- every session on the box to everyone who's logged in. See
-- backend/app/auth.py for where the header is read.
--
-- Default 'vxtto' backfills existing rows: today OAUTH2_PROXY_GITHUB_USER
-- only allows that one GitHub account to log in at all, so every session
-- created so far is genuinely theirs. Drop the default once this has run
-- against prod (new rows always pass owner explicitly from here on).
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT 'vxtto';

CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions (owner, last_active_at DESC);
