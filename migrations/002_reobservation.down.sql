DROP TABLE reobservation_intents;
DROP INDEX deletion_markers_source_identity_idx;
ALTER TABLE deletion_markers DROP COLUMN source_identity_digest;
