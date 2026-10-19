"""Source-gated observation decisions with an atomic, frozen commit boundary.

Extractors propose content; judgments only select bounded routes and identities.
Neither is a host authorization channel. No model call holds a database gate.
"""
from __future__ import annotations

from copy import deepcopy
from uuid import NAMESPACE_URL, UUID, uuid5

from liveday0.db import tenant_transaction
from liveday0.exceptions import AuthorizationRequired, IdempotencyConflict, VersionConflict
from liveday0.extraction_contract import ExtractionRequest
from liveday0.interpretation import observation_receipt
from liveday0.local_extraction import source_material, validate_and_gate
from liveday0.serialization import canonical_json, fingerprint


class _SnapshotUnavailable(ValueError):
    pass


class ObservationPipeline:
    def __init__(self, service, extractor, judgments, *, authorize_sensitive=None, candidate_limit=48):
        if type(candidate_limit) is not int or not 1 <= candidate_limit <= 64:
            raise ValueError("candidate_limit must be between 1 and 64")
        if authorize_sensitive is not None and not callable(authorize_sensitive):
            raise TypeError("authorize_sensitive must be a trusted host callback")
        self.service, self.extractor, self.judgments = service, extractor, judgments
        self.tenant_id = service.tenant_id
        self.authorize_sensitive = authorize_sensitive
        self.candidate_limit = candidate_limit

    def _sensitive_grant(self, request, proposal=None):
        if self.authorize_sensitive is None or self.authorize_sensitive(deepcopy(request), deepcopy(proposal)) is not True:
            raise AuthorizationRequired("sensitive capture requires explicit host authorization")

    @staticmethod
    def _outcome(status, reason=None, **extra):
        item = {"status": status, **extra}
        if reason is not None:
            item["reason"] = reason
        return {"outcomes": [item]}

    def _receipt(self, conn, source_key, request_fingerprint):
        self.service._check_deleted_identity(conn, source_key)
        source = conn.execute("""SELECT id,request_fingerprint FROM evidence
            WHERE tenant_id=%s AND idempotency_key=%s""", (self.tenant_id, source_key)).fetchone()
        if source is None:
            return None
        self.service._require_evidence(conn, source["id"])
        if source["request_fingerprint"] != request_fingerprint:
            raise IdempotencyConflict("source identity already belongs to a different frozen capture")
        receipt = observation_receipt(conn, self.tenant_id, source["id"])
        delta = conn.execute("""SELECT id,event_id FROM event_deltas
            WHERE tenant_id=%s AND evidence_id=%s AND state IN ('pending','absorbed') ORDER BY id""",
            (self.tenant_id, source["id"])).fetchall()
        if delta:
            receipt.update(delta_id=delta[0]["id"], event_id=delta[0]["event_id"])
        return receipt

    def _snapshot(self, conn, query):
        revision = conn.execute("SELECT revision FROM tenants WHERE id=%s", (self.tenant_id,)).fetchone()["revision"]
        # Reserve half the set for old relevant events and half for recent life.
        # Reuse indexed source FTS rather than scanning every canonical body.
        half = max(1, self.candidate_limit // 2)
        rows = conn.execute("""WITH relevant AS (
            SELECT DISTINCT c.id,c.updated_at FROM evidence e JOIN card_sources cs
              ON cs.tenant_id=e.tenant_id AND cs.evidence_id=e.id
            JOIN semantic_cards c ON c.tenant_id=cs.tenant_id AND c.id=cs.card_id
            WHERE e.tenant_id=%s AND e.status='active' AND NOT e.interpretation_revoked
              AND e.search_vector @@ plainto_tsquery('simple',%s)
              AND c.card_type='event' AND c.lifecycle IN ('active','provisional','closed')
            ORDER BY c.updated_at DESC,c.id LIMIT %s
          ), recent AS (
            SELECT id FROM semantic_cards WHERE tenant_id=%s AND card_type='event'
              AND lifecycle IN ('active','provisional','closed') ORDER BY updated_at DESC,id LIMIT %s
          ) SELECT c.id,c.current_version AS version,c.card_type AS type,v.body,c.valid_at,c.lifecycle,c.epistemic_state
            FROM semantic_cards c JOIN semantic_card_versions v
              ON v.tenant_id=c.tenant_id AND v.card_id=c.id AND v.version=c.current_version
            WHERE c.tenant_id=%s AND c.id IN (SELECT id FROM relevant UNION SELECT id FROM recent)
            ORDER BY c.id LIMIT %s""",
            (self.tenant_id, query, half, self.tenant_id, self.candidate_limit-half,
             self.tenant_id, self.candidate_limit)).fetchall()
        pending = conn.execute("""SELECT id,event_id,evidence_id,delta FROM event_deltas
            WHERE tenant_id=%s AND event_id=ANY(%s) AND state='pending'
            ORDER BY created_at,id LIMIT 129""", (self.tenant_id, [r["id"] for r in rows])).fetchall()
        if len(pending) > 128:
            raise _SnapshotUnavailable("event candidate pending input exceeds bounded snapshot")
        candidates = []
        for row in rows:
            deltas = [d for d in pending if d["event_id"] == row["id"]]
            if any(d["delta"].get("requires_restructure") for d in deltas):
                raise _SnapshotUnavailable("event candidate requires canonical catch-up")
            value = dict(row, id=str(row["id"]), body=deepcopy(row["body"]))
            for delta in deltas:
                value["body"].update(delta["delta"])
            candidates.append(value)
        frozen = canonical_json({"tenant_id": self.tenant_id, "revision": revision,
                                 "candidates": candidates, "pending": pending})
        if len(frozen.encode("utf-8")) > 64_000:
            raise _SnapshotUnavailable("event candidate snapshot exceeds bounded input")
        return candidates, frozen, revision

    def _phase_guard(self, source_key, request_fingerprint, revision):
        """Recheck after each completed external call before sending more data.

        A source or candidate deleted during an in-flight call cannot be recalled
        from that call. A changed read set prevents subsequent model requests.
        """
        with tenant_transaction(self.tenant_id, mode="read") as conn:
            try:
                prior = self._receipt(conn, source_key, request_fingerprint)
            except VersionConflict:
                return self._outcome("needs_review", "source_snapshot_changed")
            if prior is not None:
                return self._outcome("duplicate", **prior)
            current = conn.execute("SELECT revision FROM tenants WHERE id=%s", (self.tenant_id,)).fetchone()["revision"]
            if current != revision:
                return self._outcome("needs_review", "candidate_snapshot_changed")
        return None

    def capture(self, request: ExtractionRequest):
        request = deepcopy(request)
        if not isinstance(request, ExtractionRequest):
            raise TypeError("capture requires ExtractionRequest")
        # The source identity comes from the host. Fail before sending any input.
        UUID(request.source.source_id)
        if request.source.sensitivity == "secret":
            return self._outcome("rejected", "secret_source")
        if request.source.authority != "user":
            return self._outcome("rejected", "untrusted_subject_or_speaker")
        if request.source.sensitivity == "sensitive":
            self._sensitive_grant(request)
        key = str(uuid5(NAMESPACE_URL, canonical_json(["liveday0:local-source:v1",
            request.source.source_id, request.source.source_version])))
        request_fingerprint = fingerprint(canonical_json({"contract": "liveday0:capture:v1",
            "tenant_id": self.tenant_id, "request": request.to_dict()}))
        with tenant_transaction(self.tenant_id, mode="read") as conn:
            prior = self._receipt(conn, key, request_fingerprint)
            if prior is not None:
                return self._outcome("duplicate", **prior)
            try:
                candidates, frozen, revision = self._snapshot(conn, request.content)
            except _SnapshotUnavailable:
                return self._outcome("needs_review", "candidate_snapshot_unavailable")
        decisions = validate_and_gate(request, self.extractor)
        accepted = [d for d in decisions if d.reason is None]
        if len(accepted) > 1:
            return self._outcome("needs_review", "one_source_span_required")
        if not accepted:
            return {"outcomes": [{"status": "rejected", "reason": d.reason} for d in decisions]
                    or [{"status": "rejected", "reason": "no_proposal"}]}
        gated = accepted[0]
        proposal = gated.proposal
        if gated.needs_confirmation:
            self._sensitive_grant(request, proposal)
        evidence, semantic = source_material(request, gated)
        changed = self._phase_guard(key, request_fingerprint, revision)
        if changed is not None:
            return changed
        # Models receive detached inputs; in-process adapters cannot mutate the
        # frozen proposal or the candidate set that commit will verify.
        from liveday0.judgments import JudgmentFailure
        try:
            batch_judge = getattr(self.judgments, "judge_observation", None)
            batch = (batch_judge(deepcopy(request), deepcopy(proposal), deepcopy(candidates))
                     if callable(batch_judge) else None)
            classification = (batch.classification if batch is not None else
                              self.judgments.classify_observation(deepcopy(request), deepcopy(proposal)))
            disposition = classification.disposition
            if disposition == "outside_scope":
                return self._outcome("outside_scope")
            if disposition == "uncertain":
                return self._outcome("needs_review", "classification_uncertain")
            if disposition not in {"keep", "trace_only"}:
                return self._outcome("needs_review", "invalid_classification")
            if (classification.speech_mode != "direct_statement"
                    or classification.speech_mode != proposal.speech_mode or classification.subject != "user"
                    or classification.persistence not in {"situational", "durable"}):
                return self._outcome("needs_review", "classification_mismatch")
            if disposition == "keep" and (classification.persistence != proposal.scope.persistence
                    or classification.category != proposal.semantic_category):
                return self._outcome("needs_review", "classification_mismatch")
            route, target = disposition, None
            if disposition == "keep" and semantic.card_type == "event":
                changed = self._phase_guard(key, request_fingerprint, revision)
                if changed is not None:
                    return changed
                assignment = (batch.assignment if batch is not None else
                              self.judgments.assign_event(deepcopy(request), deepcopy(proposal), deepcopy(candidates)))
                if assignment is None:
                    return self._outcome("needs_review", "invalid_event_assignment")
                route = assignment.kind
                if route == "existing_event":
                    target = next((c for c in candidates if c["id"] == assignment.candidate_id), None)
                    if target is None or type(assignment.candidate_version) is not int or target["version"] != assignment.candidate_version:
                        return self._outcome("needs_review", "invalid_event_assignment")
                elif route not in {"new_event", "trace_only", "outside_scope", "uncertain"}:
                    return self._outcome("needs_review", "invalid_event_assignment")
                elif assignment.candidate_id is not None or assignment.candidate_version is not None:
                    return self._outcome("needs_review", "invalid_event_assignment")
            if route == "outside_scope":
                return self._outcome("outside_scope")
            if route == "uncertain":
                return self._outcome("needs_review", "assignment_uncertain")
            changed = self._phase_guard(key, request_fingerprint, revision)
            if changed is not None:
                return changed
            support = (batch.source_support if batch is not None else
                       self.judgments.check_source_support(deepcopy(request), deepcopy(proposal)))
            if support != "supported":
                return self._outcome("needs_review", "source_not_supported")
        except JudgmentFailure:
            return self._outcome("needs_review", "judgment_failed")
        trace = {"observation": evidence.content, "observation_boundary": "exact source span; no event inferred"} if route == "trace_only" else None
        semantics = () if trace or target else (semantic,)
        prepared = self.service._prepare_observation(evidence, trace, semantics)
        # Replay binds the original request, independently of candidate order and
        # model route. Changes to the same source/version cannot reuse a receipt.
        prepared = (*prepared[:3], request_fingerprint)
        with tenant_transaction(self.tenant_id) as conn:
            prior = self._receipt(conn, key, request_fingerprint)
            if prior is not None:
                return self._outcome("duplicate", **prior)
            try:
                _, current, _ = self._snapshot(conn, request.content)
            except _SnapshotUnavailable:
                return self._outcome("needs_review", "candidate_snapshot_unavailable")
            if current != frozen:
                return self._outcome("needs_review", "candidate_snapshot_changed")
            if target:
                delta = self.service._add_event_delta_conn(conn, UUID(target["id"]), prepared, semantic.body,
                    idempotency_key=key, expected_version=target["version"])
                receipt = self._receipt(conn, key, request_fingerprint)
                receipt.update(delta_id=delta["delta_id"], event_id=UUID(target["id"]), created=delta["created"])
            else:
                receipt = self.service._observe_conn(conn, *prepared)
        return self._outcome("stored" if receipt["created"] else "duplicate", route=route, **receipt)
