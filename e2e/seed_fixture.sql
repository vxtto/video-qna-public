-- Seeds the one video these e2e tests need: a row pointing at
-- e2e/fixtures/sample_clip.mp4 (copied into the compose stack's mounted
-- data/media/raw/ by whatever brings the stack up - see README.md), with
-- three short segments so "click a mid-transcript segment" has a real
-- middle item to click.
INSERT INTO videos (slug, title, filename, duration_seconds, license)
VALUES ('demo', 'Demo Clip (e2e fixture)', 'sample_clip.mp4', 6, 'CC0 (synthetic test fixture)')
ON CONFLICT (slug) DO UPDATE SET filename = EXCLUDED.filename;

DO $$
DECLARE
  vid INTEGER;
BEGIN
  SELECT id INTO vid FROM videos WHERE slug = 'demo';
  DELETE FROM transcript_segments WHERE video_id = vid;
  INSERT INTO transcript_segments (video_id, seq, start_ms, end_ms, text) VALUES
    (vid, 1, 0,    2000, 'segment one'),
    (vid, 2, 2000, 4000, 'segment two'),
    (vid, 3, 4000, 6000, 'segment three');
END $$;
