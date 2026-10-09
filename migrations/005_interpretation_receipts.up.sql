SET LOCAL row_security=off;
ALTER TABLE evidence ADD COLUMN interpretation_revoked boolean NOT NULL DEFAULT false;
ALTER TABLE evidence ADD COLUMN interpretation_epoch bigint NOT NULL DEFAULT 0
  CHECK (interpretation_epoch >= 0);
ALTER TABLE evidence ADD CONSTRAINT evidence_interpretation_revocation_check
  CHECK (interpretation_revoked = (interpretation_epoch > 0));

-- Original result identities, never reconstructed from mutable card_sources.
CREATE TABLE observation_receipts (
  tenant_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  trace_id uuid,
  card_ids uuid[] NOT NULL DEFAULT '{}',
  state text NOT NULL CHECK (state IN ('active','revoked','deleted')),
  PRIMARY KEY (tenant_id,evidence_id),
  FOREIGN KEY (tenant_id,evidence_id) REFERENCES evidence(tenant_id,id) ON DELETE CASCADE
);

CREATE TABLE interpretation_intents (
  tenant_id uuid NOT NULL,
  intent_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  mode text NOT NULL CHECK (mode IN ('ordinary','explicit_user_save')),
  source_version integer NOT NULL,
  source_epoch bigint NOT NULL,
  request_fingerprint text,
  provenance jsonb NOT NULL DEFAULT '{}',
  trace_id uuid,
  card_ids uuid[] NOT NULL DEFAULT '{}',
  state text NOT NULL CHECK (state IN ('active','revoked','deleted')),
  PRIMARY KEY (tenant_id,intent_id),
  FOREIGN KEY (tenant_id,evidence_id) REFERENCES evidence(tenant_id,id) ON DELETE CASCADE,
  CHECK ((state='active' AND request_fingerprint IS NOT NULL
          AND request_fingerprint ~ '^[0-9a-f]{64}$')
      OR (state IN ('revoked','deleted') AND request_fingerprint IS NULL AND provenance='{}'::jsonb))
);

-- Content-free event identity makes distinct deletions advance the epoch once each.
CREATE TABLE source_interpretation_revocations (
  tenant_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  object_kind text NOT NULL CHECK (object_kind IN ('evidence','semantic_card')),
  object_id uuid NOT NULL,
  PRIMARY KEY (tenant_id,evidence_id,object_kind,object_id),
  FOREIGN KEY (tenant_id,evidence_id) REFERENCES evidence(tenant_id,id) ON DELETE CASCADE
);

ALTER TABLE reobservation_intents DROP CONSTRAINT reobservation_intents_state_check;
ALTER TABLE reobservation_intents DROP CONSTRAINT reobservation_intents_check1;
ALTER TABLE reobservation_intents ADD CONSTRAINT reobservation_intents_state_check
  CHECK (state IN ('active','revoked','deleted'));
ALTER TABLE reobservation_intents ADD CONSTRAINT reobservation_intents_check1
  CHECK ((state='active' AND request_fingerprint IS NOT NULL AND request_fingerprint ~ '^[0-9a-f]{64}$')
      OR (state IN ('revoked','deleted') AND request_fingerprint IS NULL));

-- Reconcile provable old deletions. Do not invent receipts or request fingerprints.
INSERT INTO source_interpretation_revocations
  SELECT tenant_id,id,'evidence',id FROM evidence WHERE status='deleted'
  UNION
  SELECT DISTINCT cs.tenant_id,cs.evidence_id,'semantic_card',cs.card_id
  FROM card_sources cs JOIN semantic_cards c ON c.tenant_id=cs.tenant_id AND c.id=cs.card_id
  WHERE c.lifecycle='deleted';
UPDATE evidence e SET interpretation_revoked=true,interpretation_epoch=r.n,
  request_fingerprint=NULL,model_interpretation=NULL
FROM (SELECT tenant_id,evidence_id,count(*) AS n FROM source_interpretation_revocations
      GROUP BY tenant_id,evidence_id) r
WHERE e.tenant_id=r.tenant_id AND e.id=r.evidence_id;
UPDATE event_deltas d SET request_fingerprint=NULL FROM evidence e
  WHERE d.tenant_id=e.tenant_id AND d.evidence_id=e.id AND e.interpretation_revoked;
UPDATE reobservation_intents i SET request_fingerprint=NULL,
  state=CASE WHEN e.status='deleted' THEN 'deleted' ELSE 'revoked' END
FROM evidence e WHERE i.tenant_id=e.tenant_id AND i.new_evidence_id=e.id AND e.interpretation_revoked;

DO $$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['observation_receipts','interpretation_intents','source_interpretation_revocations']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',table_name);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',table_name);
    EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id=current_setting(''app.tenant_id'',true)::uuid) WITH CHECK (tenant_id=current_setting(''app.tenant_id'',true)::uuid)',table_name);
    EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON %I TO liveday0_app',table_name);
  END LOOP;
END $$;
