SET LOCAL row_security = off;
-- Do not silently discard parked work or restart its failure budget on rollback.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM maintenance_jobs WHERE state='waiting' OR failure_count > 0) THEN
    RAISE EXCEPTION 'maintenance waiting/failure records require explicit reconciliation before rollback';
  END IF;
END $$;
DROP INDEX maintenance_jobs_one_live_target;
CREATE UNIQUE INDEX maintenance_jobs_one_live_target ON maintenance_jobs(tenant_id,coalesce_key)
  WHERE state IN ('pending','running','retry');
ALTER TABLE maintenance_jobs DROP CONSTRAINT maintenance_jobs_state_check;
ALTER TABLE maintenance_jobs ADD CONSTRAINT maintenance_jobs_state_check
  CHECK (state IN ('pending','running','retry','succeeded','dead'));
ALTER TABLE maintenance_jobs DROP COLUMN wait_input_fingerprint;
ALTER TABLE maintenance_jobs DROP COLUMN wait_reason;
ALTER TABLE maintenance_jobs DROP COLUMN failure_count;
