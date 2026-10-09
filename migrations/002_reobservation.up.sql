-- A digest of the exact tenant-scoped source key, never a hash of life content.
ALTER TABLE deletion_markers ADD COLUMN source_identity_digest text
  CHECK (source_identity_digest IS NULL OR
         (object_kind = 'evidence' AND source_identity_digest ~ '^[0-9a-f]{64}$'));
CREATE INDEX deletion_markers_source_identity_idx
  ON deletion_markers (tenant_id, source_identity_digest)
  WHERE object_kind = 'evidence' AND source_identity_digest IS NOT NULL;

CREATE TABLE reobservation_intents (
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  intent_id uuid NOT NULL,
  deleted_evidence_id uuid NOT NULL,
  new_evidence_id uuid NOT NULL,
  request_fingerprint text,
  state text NOT NULL CHECK (state IN ('active', 'deleted')),
  PRIMARY KEY (tenant_id, intent_id),
  UNIQUE (tenant_id, new_evidence_id),
  CHECK (deleted_evidence_id <> new_evidence_id),
  CHECK ((state = 'active' AND request_fingerprint IS NOT NULL
          AND request_fingerprint ~ '^[0-9a-f]{64}$')
      OR (state = 'deleted' AND request_fingerprint IS NULL)),
  FOREIGN KEY (tenant_id, deleted_evidence_id) REFERENCES evidence(tenant_id, id),
  FOREIGN KEY (tenant_id, new_evidence_id) REFERENCES evidence(tenant_id, id)
);
ALTER TABLE reobservation_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE reobservation_intents FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON reobservation_intents
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON reobservation_intents TO liveday0_app;
