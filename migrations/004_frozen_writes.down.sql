SET LOCAL row_security=off;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM evidence WHERE request_fingerprint IS NOT NULL)
     OR EXISTS (SELECT 1 FROM event_deltas WHERE request_fingerprint IS NOT NULL) THEN
    RAISE EXCEPTION '004 downgrade requires explicit reconciliation of frozen request fingerprints';
  END IF;
END $$;
ALTER TABLE event_deltas DROP COLUMN request_fingerprint;
ALTER TABLE evidence DROP COLUMN request_fingerprint;
