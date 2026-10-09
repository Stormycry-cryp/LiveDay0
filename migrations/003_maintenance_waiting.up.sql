SET LOCAL row_security = off;
-- attempts is historical claim/check count; old waits and failures cannot be separated.
-- Start the explicit real-failure budget here without rewriting that history.
ALTER TABLE maintenance_jobs ADD COLUMN failure_count integer NOT NULL DEFAULT 0
  CHECK (failure_count >= 0);
ALTER TABLE maintenance_jobs ADD COLUMN wait_reason text
  CHECK (wait_reason IN ('dependency_pending','semantic_output_required','version_bound_rebuild_required'));
ALTER TABLE maintenance_jobs ADD COLUMN wait_input_fingerprint text
  CHECK (wait_input_fingerprint IS NULL OR wait_input_fingerprint ~ '^[0-9a-f]{64}$');
ALTER TABLE maintenance_jobs DROP CONSTRAINT maintenance_jobs_state_check;
ALTER TABLE maintenance_jobs ADD CONSTRAINT maintenance_jobs_state_check
  CHECK (state IN ('pending','running','retry','waiting','succeeded','dead'));
UPDATE maintenance_jobs SET state='waiting',locked_at=NULL,
  wait_reason=CASE last_error
    WHEN 'bounded semantic replacement required' THEN 'semantic_output_required'
    WHEN 'version-bound rebuild required after source deletion' THEN 'version_bound_rebuild_required'
    ELSE 'dependency_pending' END
WHERE state='retry' AND last_error IN (
  'bounded semantic replacement required','version-bound rebuild required after source deletion',
  'pending dependency requires canonical catch-up','unsafe support requires canonical catch-up');
DROP INDEX maintenance_jobs_one_live_target;
CREATE UNIQUE INDEX maintenance_jobs_one_live_target ON maintenance_jobs(tenant_id,coalesce_key)
  WHERE state IN ('pending','running','retry','waiting');
