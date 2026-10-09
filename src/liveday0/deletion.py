"""Bounded deletion confirmation read set; no authorization or generic CRUD.

Readers release the tenant gate before a host asks for confirmation. Deletion
rechecks this same scope under its existing write gate before its first write.
The tenant revision is deliberately conservative: even unrelated life writes
require a fresh confirmation in this small local prototype.
"""
from uuid import UUID

from liveday0.db import tenant_transaction
from liveday0.exceptions import NotFound, VersionConflict
from liveday0.serialization import canonical_json
from liveday0.types import DeletionInput


def read_deletion(tenant_id, kind, target_id):
    with tenant_transaction(tenant_id, mode='read') as conn:
        return read_deletion_conn(conn, tenant_id, kind, target_id)


def read_deletion_conn(conn, tenant_id, kind, target_id):
    if kind not in {'card', 'evidence'} or not isinstance(target_id, UUID):
        raise ValueError('deletion requires a card/evidence kind and opaque UUID')

    def rows(query, params):
        result=conn.execute(query+' LIMIT 201',params).fetchall()
        if len(result)>200:
            raise ValueError('deletion scope exceeds 200 objects per group; no truncation')
        return result

    revision=conn.execute('SELECT revision FROM tenants WHERE id=%s',(tenant_id,)).fetchone()['revision']
    if kind=='evidence':
        target=conn.execute('''SELECT id,status,version,content,object_ref,occurred_at,
            image_observation,sending_context,model_interpretation,interpretation_epoch
            FROM evidence WHERE tenant_id=%s AND id=%s''',(tenant_id,target_id)).fetchone()
        card_ids=[r['card_id'] for r in rows('''
            SELECT card_id FROM card_sources WHERE tenant_id=%s AND evidence_id=%s
            UNION SELECT unnest(card_ids) FROM observation_receipts WHERE tenant_id=%s AND evidence_id=%s
            UNION SELECT unnest(card_ids) FROM interpretation_intents WHERE tenant_id=%s AND evidence_id=%s
            ORDER BY card_id''',(tenant_id,target_id,tenant_id,target_id,tenant_id,target_id))]
        source_targets=[target_id]
    else:
        target=conn.execute('''SELECT c.id,c.card_type,c.lifecycle,c.current_version,v.body
            FROM semantic_cards c JOIN semantic_card_versions v ON v.tenant_id=c.tenant_id
            AND v.card_id=c.id AND v.version=c.current_version WHERE c.tenant_id=%s AND c.id=%s''',
            (tenant_id,target_id)).fetchone()
        card_ids=[target_id];source_targets=[]
    if target is None:
        raise NotFound('deletion target not found in tenant')
    affected={}
    affected['cards']=rows('''SELECT c.id,c.current_version,c.lifecycle,v.body FROM semantic_cards c
        JOIN semantic_card_versions v ON v.tenant_id=c.tenant_id AND v.card_id=c.id
        AND v.version=c.current_version WHERE c.tenant_id=%s AND c.id=ANY(%s) ORDER BY c.id''',(tenant_id,card_ids))
    sources=[r['evidence_id'] for r in rows('''
        SELECT evidence_id FROM card_sources WHERE tenant_id=%s AND card_id=ANY(%s)
        UNION SELECT evidence_id FROM observation_receipts WHERE tenant_id=%s AND card_ids && %s::uuid[]
        UNION SELECT evidence_id FROM interpretation_intents WHERE tenant_id=%s AND card_ids && %s::uuid[]
        ORDER BY evidence_id''',(tenant_id,card_ids,tenant_id,card_ids,tenant_id,card_ids))]
    sources=sorted(set(sources+source_targets))
    affected['revoked_sources']=rows('''SELECT id,version,status,interpretation_epoch,interpretation_revoked
        FROM evidence WHERE tenant_id=%s AND id=ANY(%s) ORDER BY id''',(tenant_id,sources))
    affected['projections']=rows('''SELECT DISTINCT p.id,p.current_version,p.lifecycle,p.scope
        FROM projections p JOIN projection_supports s ON s.tenant_id=p.tenant_id AND s.projection_id=p.id
        WHERE p.tenant_id=%s AND s.card_id=ANY(%s) ORDER BY p.id''',(tenant_id,card_ids))
    projection_ids=[r['id'] for r in affected['projections']]
    affected['traces']=rows('SELECT id,lifecycle FROM life_traces WHERE tenant_id=%s AND evidence_id=ANY(%s) ORDER BY id',(tenant_id,source_targets))
    affected['mentions']=rows('''SELECT id,state,bound_card_id FROM mentions WHERE tenant_id=%s
        AND (evidence_id=ANY(%s) OR bound_card_id=ANY(%s)) ORDER BY id''',(tenant_id,source_targets,card_ids))
    mention_ids=[r['id'] for r in affected['mentions']]
    affected['mention_candidates']=rows('''SELECT mention_id,candidate_card_id FROM mention_candidates
        WHERE tenant_id=%s AND (mention_id=ANY(%s) OR candidate_card_id=ANY(%s))
        ORDER BY mention_id,candidate_card_id''',(tenant_id,mention_ids,card_ids))
    affected['deltas']=rows('''SELECT id,event_id,evidence_id,state FROM event_deltas WHERE tenant_id=%s
        AND (event_id=ANY(%s) OR evidence_id=ANY(%s)) ORDER BY id''',(tenant_id,card_ids,sources))
    endpoints=source_targets+card_ids+projection_ids+[r['id'] for r in affected['traces']]+mention_ids
    affected['relations']=rows('''SELECT id,lifecycle FROM relations WHERE tenant_id=%s
        AND (source_evidence_id=ANY(%s) OR from_id=ANY(%s) OR to_id=ANY(%s)) ORDER BY id''',
        (tenant_id,source_targets,endpoints,endpoints))
    affected['jobs']=rows('''SELECT id,state,target_kind,target_id,baseline_version FROM maintenance_jobs
        WHERE tenant_id=%s AND target_id=ANY(%s) ORDER BY id''',(tenant_id,source_targets+card_ids+projection_ids))
    affected['observations']=rows('''SELECT evidence_id,state,trace_id,card_ids FROM observation_receipts
        WHERE tenant_id=%s AND evidence_id=ANY(%s) ORDER BY evidence_id''',(tenant_id,sources))
    affected['interpretations']=rows('''SELECT intent_id,evidence_id,state,trace_id,card_ids FROM interpretation_intents
        WHERE tenant_id=%s AND evidence_id=ANY(%s) ORDER BY intent_id''',(tenant_id,sources))
    affected['reobservations']=rows('''SELECT intent_id,new_evidence_id,state FROM reobservation_intents
        WHERE tenant_id=%s AND new_evidence_id=ANY(%s) ORDER BY intent_id''',(tenant_id,sources))
    affected['snapshots']=rows('SELECT id,state FROM recall_snapshots WHERE tenant_id=%s ORDER BY id',(tenant_id,))
    frozen=canonical_json({'contract':'liveday0:deletion-input:v1','tenant_id':tenant_id,
        'kind':kind,'target':target,'tenant_revision':revision,'affected':affected,
        'policy':'erase all target card/projection versions; revoke source interpretations; invalidate tenant snapshots'})
    if len(frozen.encode('utf-8'))>64_000:
        raise ValueError('deletion scope exceeds bounded confirmation; no truncation')
    return DeletionInput(tenant_id,kind,target_id,frozen)


def check_deletion_conn(conn, tenant_id, kind, target_id, prepared):
    if not isinstance(prepared,DeletionInput):
        raise ValueError('deletion confirmation requires a frozen source read')
    if prepared.tenant_id!=tenant_id:
        raise NotFound('deletion input belongs to another tenant')
    if prepared.kind!=kind or prepared.target_id!=target_id:
        raise VersionConflict('deletion target changed; read and confirm again')
    if read_deletion_conn(conn,tenant_id,kind,target_id)!=prepared:
        raise VersionConflict('deletion scope changed; read and confirm again')
