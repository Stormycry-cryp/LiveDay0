CREATE EXTENSION IF NOT EXISTS pgcrypto;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
    EXECUTE 'CREATE EXTENSION IF NOT EXISTS vector';
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'liveday0_app') THEN
    CREATE ROLE liveday0_app NOLOGIN;
  END IF;
END
$$;

CREATE TABLE tenants (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  revision bigint NOT NULL DEFAULT 0
);

CREATE TABLE evidence (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  modality text NOT NULL CHECK (modality IN ('text', 'image', 'object')),
  source_kind text NOT NULL,
  content text,
  object_ref text,
  occurred_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  image_observation text,
  sending_context text,
  model_interpretation text,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'corrected', 'deleted')),
  version integer NOT NULL DEFAULT 1 CHECK (version > 0),
  idempotency_key text,
  search_vector tsvector GENERATED ALWAYS AS (
    to_tsvector('simple', coalesce(content, '') || ' ' || coalesce(image_observation, '') || ' ' || coalesce(sending_context, ''))
  ) STORED,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, idempotency_key),
  CHECK (status = 'deleted' OR content IS NOT NULL OR object_ref IS NOT NULL)
);

CREATE TABLE life_traces (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  observation text NOT NULL,
  observation_boundary text NOT NULL,
  accessibility real NOT NULL DEFAULT 0.1 CHECK (accessibility >= 0 AND accessibility <= 1),
  lifecycle text NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('active', 'absorbed', 'invalidated', 'deleted')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, evidence_id),
  FOREIGN KEY (tenant_id, evidence_id) REFERENCES evidence(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE semantic_cards (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  canonical_key text NOT NULL,
  card_type text NOT NULL CHECK (card_type IN ('event', 'fact', 'prospective')),
  lifecycle text NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('provisional', 'active', 'closed', 'invalidated', 'deleted')),
  epistemic_state text NOT NULL DEFAULT 'confirmed' CHECK (epistemic_state IN ('candidate', 'provisional', 'confirmed', 'corrected', 'superseded')),
  valid_at timestamptz NOT NULL,
  current_version integer NOT NULL DEFAULT 1 CHECK (current_version > 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, canonical_key)
);

CREATE TABLE semantic_card_versions (
  tenant_id uuid NOT NULL,
  card_id uuid NOT NULL,
  version integer NOT NULL,
  body jsonb NOT NULL,
  lifecycle text NOT NULL,
  epistemic_state text NOT NULL,
  valid_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, card_id, version),
  FOREIGN KEY (tenant_id, card_id) REFERENCES semantic_cards(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE card_sources (
  tenant_id uuid NOT NULL,
  card_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  source_role text NOT NULL DEFAULT 'support' CHECK (source_role IN ('support', 'correction', 'counterevidence')),
  PRIMARY KEY (tenant_id, card_id, evidence_id, source_role),
  FOREIGN KEY (tenant_id, card_id) REFERENCES semantic_cards(tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, evidence_id) REFERENCES evidence(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE event_deltas (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  event_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  delta jsonb NOT NULL,
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'absorbed', 'invalidated')),
  idempotency_key text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  absorbed_at timestamptz,
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, event_id, idempotency_key),
  FOREIGN KEY (tenant_id, event_id) REFERENCES semantic_cards(tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, evidence_id) REFERENCES evidence(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE mentions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  evidence_id uuid NOT NULL,
  surface_text text NOT NULL,
  state text NOT NULL DEFAULT 'unbound' CHECK (state IN ('unbound', 'bound', 'invalidated')),
  bound_card_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, evidence_id) REFERENCES evidence(tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, bound_card_id) REFERENCES semantic_cards(tenant_id, id)
);

CREATE TABLE mention_candidates (
  tenant_id uuid NOT NULL,
  mention_id uuid NOT NULL,
  candidate_card_id uuid NOT NULL,
  rank integer NOT NULL,
  reason text NOT NULL,
  confidence real CHECK (confidence >= 0 AND confidence <= 1),
  PRIMARY KEY (tenant_id, mention_id, candidate_card_id),
  FOREIGN KEY (tenant_id, mention_id) REFERENCES mentions(tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, candidate_card_id) REFERENCES semantic_cards(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE relations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  from_kind text NOT NULL,
  from_id uuid NOT NULL,
  to_kind text NOT NULL,
  to_id uuid NOT NULL,
  family text NOT NULL CHECK (family IN ('evidence_support', 'temporal_causal', 'event_thread', 'state_invalidation', 'context_involvement')),
  relation_type text NOT NULL,
  annotation text,
  strength real CHECK (strength >= 0 AND strength <= 1),
  lifecycle text NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('active', 'superseded', 'invalidated', 'deleted')),
  source_evidence_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, from_kind, from_id, to_kind, to_id, relation_type),
  FOREIGN KEY (tenant_id, source_evidence_id) REFERENCES evidence(tenant_id, id)
);

CREATE TABLE projections (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  projection_key text NOT NULL,
  projection_type text NOT NULL CHECK (projection_type IN ('current_state', 'life_thread', 'relationship')),
  scope text NOT NULL,
  lifecycle text NOT NULL DEFAULT 'active' CHECK (lifecycle IN ('active', 'dormant', 'invalidated', 'deleted')),
  epistemic_state text NOT NULL DEFAULT 'confirmed',
  current_version integer NOT NULL DEFAULT 1,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, projection_key)
);

CREATE TABLE projection_versions (
  tenant_id uuid NOT NULL,
  projection_id uuid NOT NULL,
  version integer NOT NULL,
  body jsonb NOT NULL,
  lifecycle text NOT NULL,
  epistemic_state text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, projection_id, version),
  FOREIGN KEY (tenant_id, projection_id) REFERENCES projections(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE projection_supports (
  tenant_id uuid NOT NULL,
  projection_id uuid NOT NULL,
  card_id uuid NOT NULL,
  support_role text NOT NULL CHECK (support_role IN ('support', 'counterevidence')),
  PRIMARY KEY (tenant_id, projection_id, card_id, support_role),
  FOREIGN KEY (tenant_id, projection_id) REFERENCES projections(tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, card_id) REFERENCES semantic_cards(tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE maintenance_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  job_type text NOT NULL CHECK (job_type IN ('event_rewrite', 'projection_resynthesis', 'candidate_discovery')),
  target_kind text NOT NULL,
  target_id uuid,
  coalesce_key text NOT NULL,
  baseline_version integer,
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'retry', 'succeeded', 'dead')),
  attempts integer NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id)
);

CREATE UNIQUE INDEX maintenance_jobs_one_live_target
  ON maintenance_jobs (tenant_id, coalesce_key)
  WHERE state IN ('pending', 'running', 'retry');

CREATE TABLE recall_snapshots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  tenant_revision bigint NOT NULL,
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'invalidated', 'expired')),
  context jsonb NOT NULL,
  expansion_store jsonb NOT NULL DEFAULT '{}'::jsonb,
  referenced_ids uuid[] NOT NULL DEFAULT '{}',
  degraded_reasons text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  invalidated_at timestamptz,
  UNIQUE (tenant_id, id)
);

CREATE TABLE deletion_markers (
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  object_kind text NOT NULL,
  object_id uuid NOT NULL,
  reason_code text NOT NULL,
  deleted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, object_kind, object_id)
);

CREATE INDEX evidence_search_idx ON evidence USING gin (search_vector);
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
    EXECUTE 'ALTER TABLE evidence ADD COLUMN embedding vector(8)';
    EXECUTE 'CREATE INDEX evidence_embedding_idx ON evidence USING hnsw (embedding vector_cosine_ops)';
  END IF;
END
$$;
CREATE INDEX semantic_cards_active_idx ON semantic_cards (tenant_id, card_type, lifecycle, valid_at DESC);
CREATE INDEX relations_from_idx ON relations (tenant_id, from_kind, from_id, family) WHERE lifecycle = 'active';
CREATE INDEX relations_to_idx ON relations (tenant_id, to_kind, to_id, family) WHERE lifecycle = 'active';
CREATE INDEX deltas_pending_idx ON event_deltas (tenant_id, event_id, created_at) WHERE state = 'pending';
CREATE INDEX jobs_ready_idx ON maintenance_jobs (tenant_id, state, available_at);

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenants USING (id = current_setting('app.tenant_id', true)::uuid) WITH CHECK (id = current_setting('app.tenant_id', true)::uuid);

DO $$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'evidence', 'life_traces', 'semantic_cards', 'semantic_card_versions',
    'card_sources', 'event_deltas', 'mentions', 'mention_candidates',
    'relations', 'projections', 'projection_versions', 'projection_supports',
    'maintenance_jobs', 'recall_snapshots', 'deletion_markers'
  ]
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)::uuid) WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true)::uuid)',
      table_name
    );
  END LOOP;
END
$$;

GRANT USAGE ON SCHEMA public TO liveday0_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO liveday0_app;
