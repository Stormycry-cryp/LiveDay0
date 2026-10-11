"""Read-only operational retention preview; life memory is outside this policy."""
from uuid import UUID

from liveday0.db import tenant_transaction


def preview_retention(tenant_id: UUID, *, snapshot_days: int | None = None,
                      job_days: int | None = None, row_warning: int = 10_000,
                      sample_limit: int = 20) -> dict:
    if not isinstance(tenant_id, UUID):
        raise ValueError("retention preview requires a trusted tenant UUID")
    for days in (snapshot_days, job_days):
        if days is not None and (type(days) is not int or not 1 <= days <= 36_500):
            raise ValueError("retention days must be an integer from 1 to 36500, or omitted")
    if type(row_warning) is not int or row_warning < 1:
        raise ValueError("row warning must be a positive integer")
    if type(sample_limit) is not int or not 1 <= sample_limit <= 100:
        raise ValueError("sample limit must be an integer from 1 to 100")
    with tenant_transaction(tenant_id, mode="read") as conn:
        # Explicit operator inspection only. Count and samples share one SQL
        # snapshot, with a finite query deadline; do not scan this on every turn.
        conn.execute("SET LOCAL statement_timeout='2000ms'")
        row = conn.execute(
            """WITH settings AS (
              SELECT transaction_timestamp() AS as_of,
                %(snapshot_days)s::integer AS snapshot_days,%(job_days)s::integer AS job_days
            ), snapshot_rows AS MATERIALIZED (
              SELECT s.id,s.invalidated_at,
                CASE WHEN s.state='active' THEN 'active_snapshot'
                  WHEN s.invalidated_at IS NULL THEN 'unknown_invalidation_time'
                  WHEN p.snapshot_days IS NULL THEN 'policy_disabled'
                  WHEN s.invalidated_at > p.as_of-make_interval(days=>p.snapshot_days) THEN 'recent'
                  ELSE 'eligible' END AS reason
              FROM recall_snapshots s CROSS JOIN settings p WHERE s.tenant_id=%(tenant)s
            ), job_rows AS MATERIALIZED (
              SELECT j.id,j.updated_at,
                CASE WHEN j.state IN ('pending','running','retry','waiting') THEN 'unfinished_task'
                  WHEN j.state='dead' OR j.failure_count>0 OR j.last_error IS NOT NULL THEN 'failure_history'
                  WHEN j.job_type NOT IN ('event_rewrite','projection_resynthesis') THEN 'unsupported_terminal_type'
                  WHEN j.state<>'succeeded' OR j.locked_at IS NOT NULL OR j.wait_reason IS NOT NULL
                    OR j.wait_input_fingerprint IS NOT NULL THEN 'terminal_bookkeeping'
                  WHEN row_number() OVER (PARTITION BY j.coalesce_key
                    ORDER BY j.created_at DESC,j.id DESC)=1 THEN 'scheduler_anchor'
                  WHEN p.job_days IS NULL THEN 'policy_disabled'
                  WHEN j.updated_at > p.as_of-make_interval(days=>p.job_days) THEN 'recent'
                  ELSE 'eligible' END AS reason
              FROM maintenance_jobs j CROSS JOIN settings p WHERE j.tenant_id=%(tenant)s
            ) SELECT (SELECT as_of FROM settings) AS as_of,
              jsonb_build_object(
                'total',count(*),'eligible',count(*) FILTER (WHERE reason='eligible'),
                'protected',count(*) FILTER (WHERE reason<>'eligible'),
                'protected_reasons',coalesce((SELECT jsonb_object_agg(reason,n) FROM
                  (SELECT reason,count(*) n FROM snapshot_rows WHERE reason<>'eligible' GROUP BY reason) r),'{}'::jsonb),
                'samples',coalesce((SELECT jsonb_agg(to_jsonb(s)) FROM
                  (SELECT id,invalidated_at FROM snapshot_rows WHERE reason='eligible'
                   ORDER BY invalidated_at,id LIMIT %(sample_limit)s) s),'[]'::jsonb)
              ) AS snapshots,
              (SELECT jsonb_build_object(
                'total',count(*),'eligible',count(*) FILTER (WHERE reason='eligible'),
                'protected',count(*) FILTER (WHERE reason<>'eligible'),
                'protected_reasons',coalesce((SELECT jsonb_object_agg(reason,n) FROM
                  (SELECT reason,count(*) n FROM job_rows WHERE reason<>'eligible' GROUP BY reason) r),'{}'::jsonb),
                'samples',coalesce((SELECT jsonb_agg(to_jsonb(j)) FROM
                  (SELECT id,updated_at FROM job_rows WHERE reason='eligible'
                   ORDER BY updated_at,id LIMIT %(sample_limit)s) j),'[]'::jsonb)
              ) FROM job_rows) AS jobs FROM snapshot_rows""",
            {'tenant': tenant_id, 'snapshot_days': snapshot_days, 'job_days': job_days,
             'sample_limit': sample_limit},
        ).fetchone()
    for kind in ('snapshots', 'jobs'):
        row[kind]['samples_truncated'] = row[kind]['eligible'] > len(row[kind]['samples'])
        row[kind]['row_warning_reached'] = row[kind]['total'] >= row_warning
    return {'dry_run': True, 'as_of': row['as_of'],
            'policy': {'snapshot_days': snapshot_days, 'job_days': job_days,
                       'row_warning': row_warning, 'sample_limit': sample_limit},
            'snapshots': row['snapshots'], 'jobs': row['jobs'],
            'mutations': 0,
            'scope': 'operational rows only; no automatic expiry, hard capacity limit or life-memory retention change'}
