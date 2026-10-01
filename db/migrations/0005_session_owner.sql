-- Per-user session scoping: stamps the caller's identity (see
-- backend/app/auth.py) onto sessions so /api/sessions lists only the
-- caller's own chats. The default backfills rows created before this
-- column existed; new rows always pass owner explicitly.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT 'dev';

CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions (owner, last_active_at DESC);
