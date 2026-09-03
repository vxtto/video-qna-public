-- Chat tray needs something to label each row with. No separate write
-- path: queries.record_turn stamps it from the session's first user
-- message (COALESCE'd so later turns never overwrite it) - see
-- backend/app/queries.py. Nullable: a session with zero turns (shouldn't
-- exist in practice - sessions are only created inline with a first
-- /api/chat call, see main.py:_resolve_session) just wouldn't show one.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title TEXT;
