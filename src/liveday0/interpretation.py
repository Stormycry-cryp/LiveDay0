"""Internal source interpretation, original receipts, and deletion revocation.

This is not an extractor or a public authorization endpoint. The optional host
callback is deliberately absent by default; model-authored flags grant nothing.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import re
from uuid import UUID

from psycopg.types.json import Jsonb

from liveday0.db import tenant_transaction
from liveday0.exceptions import (
    AuthorizationRequired, DeletedSource, IdempotencyConflict, InterpretationRevoked,
    NotFound, ReceiptUnavailable, VersionConflict,
)
from liveday0.serialization import canonical_json, fingerprint
from liveday0.types import EvidenceInterpretationInput, ExplicitSaveAuthorization


def observation_receipt(conn, tenant_id, evidence_id, *, created=False):
    row = conn.execute("SELECT * FROM observation_receipts WHERE tenant_id=%s AND evidence_id=%s",
                       (tenant_id,evidence_id)).fetchone()
    if row is None:
        raise ReceiptUnavailable("legacy observation has no original receipt")
    if row["state"] == "deleted":
        raise DeletedSource("observation receipt was deleted")
    if row["state"] != "active":
        raise InterpretationRevoked("observation receipt was revoked")
    return {"evidence_id":evidence_id,"trace_id":row["trace_id"],
            "card_ids":list(row["card_ids"]),"created":created}


def sources_for_card(conn, tenant_id, card_id):
    # Original receipt IDs survive only as opaque deletion/replay tombstones.
    # They close the gap when later mutable source edges no longer include the creator.
    return [row["evidence_id"] for row in conn.execute(
        """SELECT evidence_id FROM card_sources WHERE tenant_id=%s AND card_id=%s
        UNION SELECT evidence_id FROM observation_receipts WHERE tenant_id=%s AND %s=ANY(card_ids)
        UNION SELECT evidence_id FROM interpretation_intents WHERE tenant_id=%s AND %s=ANY(card_ids)
        ORDER BY evidence_id""", (tenant_id,card_id,tenant_id,card_id,tenant_id,card_id))]


def revoke_source(conn, tenant_id, evidence_id, *, object_kind, object_id, source_deleted=False):
    """Idempotent cleanup; each opaque deletion event advances this source once."""
    new = conn.execute(
        """INSERT INTO source_interpretation_revocations VALUES (%s,%s,%s,%s)
        ON CONFLICT DO NOTHING RETURNING evidence_id""",
        (tenant_id,evidence_id,object_kind,object_id),
    ).fetchone()
    if new:
        conn.execute("""UPDATE evidence SET interpretation_revoked=true,
            interpretation_epoch=interpretation_epoch+1 WHERE tenant_id=%s AND id=%s""", (tenant_id,evidence_id))
    deleted = source_deleted or conn.execute("SELECT status FROM evidence WHERE tenant_id=%s AND id=%s",
        (tenant_id,evidence_id)).fetchone()["status"] == "deleted"
    state = "deleted" if deleted else "revoked"
    conn.execute("""UPDATE evidence SET request_fingerprint=NULL,model_interpretation=NULL
        WHERE tenant_id=%s AND id=%s AND (request_fingerprint IS NOT NULL OR model_interpretation IS NOT NULL)""",
        (tenant_id,evidence_id))
    conn.execute("""UPDATE event_deltas SET request_fingerprint=NULL
        WHERE tenant_id=%s AND evidence_id=%s AND request_fingerprint IS NOT NULL""", (tenant_id,evidence_id))
    conn.execute("""UPDATE observation_receipts SET state=%s WHERE tenant_id=%s AND evidence_id=%s
        AND state NOT IN ('deleted',%s)""", (state,tenant_id,evidence_id,state))
    conn.execute("""UPDATE interpretation_intents SET state=CASE WHEN state='deleted' THEN state ELSE %s END,
        request_fingerprint=NULL,provenance='{}'::jsonb WHERE tenant_id=%s AND evidence_id=%s
        AND (state NOT IN ('deleted',%s) OR request_fingerprint IS NOT NULL OR provenance<>'{}'::jsonb)""",
        (state,tenant_id,evidence_id,state))
    conn.execute("""UPDATE reobservation_intents SET state=CASE WHEN state='deleted' THEN state ELSE %s END,
        request_fingerprint=NULL WHERE tenant_id=%s AND new_evidence_id=%s
        AND (state NOT IN ('deleted',%s) OR request_fingerprint IS NOT NULL)""", (state,tenant_id,evidence_id,state))


class InterpretationEngine:
    def __init__(self, service, explicit_save_authorizer=None):
        if explicit_save_authorizer is not None and not callable(explicit_save_authorizer):
            raise TypeError("explicit_save_authorizer must be a trusted read-only host callback")
        self.service = service
        self.tenant_id = service.tenant_id
        self._authorize = explicit_save_authorizer

    def _host_check(self, intent_id, phase, request):
        if not isinstance(intent_id, UUID):
            raise ValueError("explicit save requires an opaque UUID intent")
        check = ExplicitSaveAuthorization(self.tenant_id,intent_id,phase,canonical_json(request))
        if self._authorize is None or self._authorize(check) is not True:
            raise AuthorizationRequired("trusted host authorization is required; model flags are not authorization")

    def read(self, evidence_id):
        with tenant_transaction(self.tenant_id, mode="read") as conn:
            return self._read_conn(conn,evidence_id,"ordinary")

    def read_explicit(self, evidence_id, *, explicit_save_intent_id):
        self._host_check(explicit_save_intent_id,"read",{
            "evidence_id":evidence_id,"purpose":"one_explicit_reinterpretation"})
        with tenant_transaction(self.tenant_id, mode="read") as conn:
            return self._read_conn(conn,evidence_id,"explicit_user_save",explicit_save_intent_id)

    def _read_conn(self, conn, evidence_id, mode, explicit_save_intent_id=None):
        source = conn.execute("""SELECT id,version,status,interpretation_revoked,interpretation_epoch,
            modality,source_kind,content,object_ref,occurred_at,image_observation,sending_context,model_interpretation
            FROM evidence WHERE tenant_id=%s AND id=%s""", (self.tenant_id,evidence_id)).fetchone()
        if source is None:
            raise NotFound("source not found in tenant")
        if source["status"] == "deleted":
            raise DeletedSource("deleted source requires explicit reobserve_deleted with new source material")
        if mode == "ordinary" and source["interpretation_revoked"]:
            raise InterpretationRevoked("source interpretation was revoked")
        if mode == "explicit_user_save" and not source["interpretation_revoked"]:
            raise VersionConflict("explicit recovery requires a revoked source")
        payload = {"contract":"liveday0:evidence-interpretation:v1","tenant_id":self.tenant_id,
            "mode":mode,"explicit_save_intent_id":explicit_save_intent_id,"source":source}
        frozen = canonical_json(payload)
        if len(frozen.encode("utf-8")) > 64_000:
            raise ValueError("interpretation source exceeds the bounded input; no automatic truncation")
        return EvidenceInterpretationInput(self.tenant_id,evidence_id,frozen)

    def _proposals(self, trace, semantics, provenance):
        trace, semantics, provenance = deepcopy((trace,list(semantics),{} if provenance is None else provenance))
        if not semantics and not trace:
            raise ValueError("interpretation requires a trace or semantic proposals")
        if len(semantics) > 16:
            raise ValueError("interpretation supports at most 16 semantic proposals")
        self.service._validate_semantics(semantics)
        for item in semantics:
            if item.canonical_key is not None and (not isinstance(item.canonical_key,str) or not item.canonical_key):
                raise ValueError("canonical key must be a nonempty string or None")
        if trace is not None:
            if not isinstance(trace,dict) or set(trace)-{"observation","observation_boundary","accessibility"}:
                raise ValueError("trace requires the closed internal trace shape")
            if not isinstance(trace.get("observation"),str) or not trace["observation"]:
                raise ValueError("trace requires an observation")
            if "observation_boundary" in trace and not isinstance(trace["observation_boundary"],str):
                raise ValueError("trace boundary must be text")
        allowed = {"producer_id","model_id","extractor_version","policy_version"}
        if not isinstance(provenance,dict) or set(provenance)-allowed or any(
            not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}",value)
            for value in provenance.values()
        ):
            raise ValueError("provenance only accepts bounded producer identifiers; no bodies or authorization flags")
        body = {"trace":trace,"semantics":[asdict(item) for item in semantics],"provenance":provenance}
        if len(canonical_json(body).encode("utf-8")) > 64_000:
            raise ValueError("interpretation output exceeds the bounded contract")
        return trace,semantics,provenance,body

    def commit(self, prepared, *, intent_id, trace=None, semantics=(), provenance=None):
        return self._commit(prepared,intent_id,"ordinary",trace,semantics,provenance)

    def commit_explicit(self, prepared, *, explicit_save_intent_id, trace=None, semantics=(), provenance=None):
        return self._commit(prepared,explicit_save_intent_id,"explicit_user_save",trace,semantics,provenance)

    def _commit(self, prepared, intent_id, mode, trace, semantics, provenance):
        if not isinstance(prepared,EvidenceInterpretationInput) or not isinstance(intent_id,UUID):
            raise ValueError("interpretation requires a frozen source read and opaque UUID intent")
        if prepared.tenant_id != self.tenant_id:
            raise NotFound("prepared source belongs to another tenant")
        payload = prepared.payload
        if payload.get("contract") != "liveday0:evidence-interpretation:v1" or payload.get("mode") != mode:
            raise VersionConflict("interpretation input contract or mode mismatch")
        authorization_id = intent_id if mode == "explicit_user_save" else None
        if payload.get("explicit_save_intent_id") != (str(authorization_id) if authorization_id else None):
            raise VersionConflict("explicit save intent does not match the source read")
        trace,semantics,provenance,body = self._proposals(trace,semantics,provenance)
        request = {"contract":"liveday0:interpretation-intent:v1","intent_id":intent_id,
            "input":payload,"output":body}
        request_fingerprint = fingerprint(canonical_json(request))
        # This host check is read-only and outside the database/model lock.
        if mode == "explicit_user_save":
            self._host_check(intent_id,"commit",request)
        with tenant_transaction(self.tenant_id) as conn:
            previous = conn.execute("SELECT * FROM interpretation_intents WHERE tenant_id=%s AND intent_id=%s",
                                    (self.tenant_id,intent_id)).fetchone()
            if previous and previous["state"] != "active":
                raise InterpretationRevoked("interpretation intent was erased; it cannot be reused")
            if previous and previous["request_fingerprint"] != request_fingerprint:
                raise IdempotencyConflict("intent was used for a different frozen interpretation")
            current = self._read_conn(conn,prepared.evidence_id,mode,authorization_id)
            if current != prepared:
                raise VersionConflict("source input changed; read and interpret again")
            if previous:
                return {"evidence_id":previous["evidence_id"],"trace_id":previous["trace_id"],
                    "card_ids":list(previous["card_ids"]),"created":False}
            if trace and conn.execute("SELECT 1 FROM life_traces WHERE tenant_id=%s AND evidence_id=%s",
                                      (self.tenant_id,prepared.evidence_id)).fetchone():
                raise VersionConflict("a source already has a trace; interpretation cannot overwrite it")
            keys = [item.canonical_key or f"{item.card_type}:interpretation:{intent_id}:{index}"
                    for index,item in enumerate(semantics)]
            if len(set(keys)) != len(keys) or conn.execute(
                "SELECT 1 FROM semantic_cards WHERE tenant_id=%s AND canonical_key=ANY(%s)",
                (self.tenant_id,keys),
            ).fetchone():
                raise VersionConflict("canonical identity exists; resolve its target explicitly")
            trace_id = self.service._create_trace_conn(conn,prepared.evidence_id,trace) if trace else None
            card_ids = [self.service._create_card(conn,prepared.evidence_id,key,item)
                        for key,item in zip(keys,semantics)]
            conn.execute("""INSERT INTO interpretation_intents(
                tenant_id,intent_id,evidence_id,mode,source_version,source_epoch,request_fingerprint,
                provenance,trace_id,card_ids,state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')""",
                (self.tenant_id,intent_id,prepared.evidence_id,mode,payload["source"]["version"],
                 payload["source"]["interpretation_epoch"],request_fingerprint,Jsonb(provenance),trace_id,card_ids))
            # Local discovery notification; the existing worker remains explicitly unimplemented.
            self.service._enqueue_job_conn(conn,job_type="candidate_discovery",target_kind="evidence",
                target_id=prepared.evidence_id,coalesce_key=f"candidate_discovery:{prepared.evidence_id}",
                baseline_version=payload["source"]["version"],available_after_seconds=0)
            self.service._bump_revision(conn)
            return {"evidence_id":prepared.evidence_id,"trace_id":trace_id,"card_ids":card_ids,"created":True}
