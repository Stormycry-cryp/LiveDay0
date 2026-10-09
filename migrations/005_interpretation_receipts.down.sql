SET LOCAL row_security=off;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM observation_receipts)
     OR EXISTS (SELECT 1 FROM interpretation_intents)
     OR EXISTS (SELECT 1 FROM source_interpretation_revocations)
     OR EXISTS (SELECT 1 FROM evidence WHERE interpretation_revoked OR interpretation_epoch <> 0)
     OR EXISTS (SELECT 1 FROM reobservation_intents WHERE state='revoked') THEN
    RAISE EXCEPTION '005 downgrade requires explicit reconciliation of receipts and interpretation revocations';
  END IF;
END $$;
DROP TABLE source_interpretation_revocations,interpretation_intents,observation_receipts;
ALTER TABLE evidence DROP CONSTRAINT evidence_interpretation_revocation_check;
ALTER TABLE evidence DROP COLUMN interpretation_revoked;
ALTER TABLE evidence DROP COLUMN interpretation_epoch;
ALTER TABLE reobservation_intents DROP CONSTRAINT reobservation_intents_state_check;
ALTER TABLE reobservation_intents DROP CONSTRAINT reobservation_intents_check1;
ALTER TABLE reobservation_intents ADD CONSTRAINT reobservation_intents_state_check
  CHECK (state IN ('active','deleted'));
ALTER TABLE reobservation_intents ADD CONSTRAINT reobservation_intents_check1
  CHECK ((state='active' AND request_fingerprint IS NOT NULL AND request_fingerprint ~ '^[0-9a-f]{64}$')
      OR (state='deleted' AND request_fingerprint IS NULL));
