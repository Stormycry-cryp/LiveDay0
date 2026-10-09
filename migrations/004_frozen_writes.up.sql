-- No backfill: current mutable data cannot reconstruct the original request.
ALTER TABLE evidence ADD COLUMN request_fingerprint text
  CHECK (request_fingerprint IS NULL OR request_fingerprint ~ '^[0-9a-f]{64}$');
ALTER TABLE event_deltas ADD COLUMN request_fingerprint text
  CHECK (request_fingerprint IS NULL OR request_fingerprint ~ '^[0-9a-f]{64}$');
