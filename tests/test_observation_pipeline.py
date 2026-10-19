from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from liveday0.core import MemoryService
from liveday0.db import tenant_transaction
from liveday0.exceptions import AuthorizationRequired, DeletedSource, IdempotencyConflict, VersionConflict
from liveday0.extraction_contract import (
    ExtractionRequest, ProposalBatch, ProposalSource, SourceSpan, SemanticProposal,
    SubjectRef, ProposalScope, RevisionRef, ProposedSemanticInput,
)
from liveday0.judgments import Classification, EventAssignment, JudgmentFailure, ObservationJudgment
from liveday0.local_extraction import PRODUCER
from liveday0.observation_pipeline import ObservationPipeline
from tests.helpers import evidence, event


def request(text="I finished moving with my friend.", **kwargs):
    return ExtractionRequest.build(str(uuid4()), "1", text, datetime.now(timezone.utc), **kwargs)


class Extractor:
    def __init__(self, card_type="event", mutate=None):
        self.card_type, self.mutate, self.calls = card_type, mutate, 0

    def extract(self, req):
        self.calls += 1
        bodies = {"event": {"goal_context": "moving", "current_result": "finished moving"},
                  "fact": {"proposition": "I prefer quiet mornings", "scope": "my mornings"},
                  "prospective": {"item": "visit my friend", "status": "planned"}}
        category = {"event": "event", "fact": "preference", "prospective": "commitment"}[self.card_type]
        proposal = SemanticProposal("p1", ProposalSource(req.source.source_id, req.source.source_version,
            req.content_digest, SourceSpan.from_text(req.chunk_start, req.content)), "user", SubjectRef("user", "self"),
            "direct_statement", category, ProposalScope("durable", "self"), 1.0, "asserted", "ordinary",
            "implicit_candidate", RevisionRef("create", "extractor-key"), PRODUCER,
            ProposedSemanticInput(self.card_type, bodies[self.card_type], "active", "confirmed", "extractor-key", req.occurred_at))
        if self.mutate:
            proposal = self.mutate(proposal)
        return ProposalBatch(req.fingerprint, (proposal,))


class Judgments:
    def __init__(self, disposition="keep", assignment=None, support="supported", on_assign=None, on_support=None):
        self.disposition, self.assignment, self.support = disposition, assignment, support
        self.on_assign, self.on_support = on_assign, on_support
        self.calls, self.candidates = [], []

    def classify_observation(self, req, proposal):
        self.calls.append("classify")
        return Classification(self.disposition, proposal.speech_mode, "user", proposal.scope.persistence, proposal.semantic_category)

    def assign_event(self, req, proposal, candidates):
        self.calls.append("assign")
        self.candidates = candidates
        if self.on_assign:
            self.on_assign(candidates)
        return self.assignment or EventAssignment("new_event")

    def check_source_support(self, req, proposal):
        self.calls.append("support")
        if self.on_support:
            self.on_support()
        if isinstance(self.support, Exception):
            raise self.support
        return self.support

    def rank_recall_candidates(self, query, candidates):
        return []


def outcome(pipeline, req=None):
    return pipeline.capture(req or request())["outcomes"][0]


def counts(service):
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        return {table: conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
                for table in ("evidence", "semantic_cards", "life_traces", "event_deltas")}


def seed_event(service, content="moving", result="waiting for help"):
    return service.observe(evidence(content), semantics=[event({"goal_context": "moving", "current_result": result})])["card_ids"][0]


@pytest.mark.parametrize("change", ["delete", "correct"])
def test_batch_model_changed_candidate_is_not_committed(service, change):
    target = seed_event(service)
    after_change = []

    class BatchJudgments(Judgments):
        def judge_observation(self, req, proposal, candidates):
            if change == "delete":
                service.delete_card(target)
            else:
                service.correct_card(target, evidence("new confirmed result"),
                    {"goal_context": "moving", "current_result": "corrected"}, expected_version=1)
            after_change.append(counts(service))
            return ObservationJudgment(Classification("keep", proposal.speech_mode, "user",
                proposal.scope.persistence, proposal.semantic_category),
                EventAssignment("existing_event", str(target), 1), "supported")

        def classify_observation(self, *_):
            raise AssertionError("batch must not repeat classification")

    result = outcome(ObservationPipeline(service, Extractor(), BatchJudgments()))
    assert result["status"] == "needs_review" and result["reason"] == "candidate_snapshot_changed"
    assert counts(service) == after_change[0]


def test_public_capture_new_event_and_replay_before_extractor(service):
    judgments, extractor, req = Judgments(), Extractor(), request()
    configured = MemoryService(service.tenant_id, judgments=judgments)
    first = configured.capture(req, extractor=extractor)["outcomes"][0]
    assert first["status"] == "stored" and first["route"] == "new_event"
    seed_event(service, "unrelated life")  # Candidate set changes must not change replay identity.
    second = configured.capture(req, extractor=extractor)["outcomes"][0]
    assert second["status"] == "duplicate" and second["card_ids"] == first["card_ids"]
    assert extractor.calls == 1 and judgments.calls == ["classify", "assign", "support"]
    with pytest.raises(ValueError, match="configured judgments"):
        service.capture(request(), extractor=extractor)


def test_existing_event_appends_atomic_delta_and_replays(service):
    target = seed_event(service)
    judgments = Judgments(assignment=EventAssignment("existing_event", str(target), 1))
    pipeline, req = ObservationPipeline(service, Extractor(), judgments), request()
    first = outcome(pipeline, req)
    assert first["status"] == "stored" and first["event_id"] == target
    assert service.effective_event(target)["body"]["current_result"] == "finished moving"
    second = outcome(pipeline, req)
    assert second["status"] == "duplicate" and second["delta_id"] == first["delta_id"]
    assert counts(service) == {"evidence": 2, "semantic_cards": 1, "life_traces": 0, "event_deltas": 1}


def test_candidate_effective_state_includes_pending_delta(service):
    target = seed_event(service)
    service.add_event_delta(target, evidence("friend has arrived"), {"current_result": "friend arrived"}, idempotency_key="arrival")
    judgments = Judgments()
    assert outcome(ObservationPipeline(service, Extractor(), judgments))["status"] == "stored"
    assert next(c for c in judgments.candidates if c["id"] == str(target))["body"]["current_result"] == "friend arrived"


@pytest.mark.parametrize("assignment", [
    EventAssignment("existing_event", str(uuid4()), 1),
    EventAssignment("existing_event", "cross-tenant-placeholder", 1),
    EventAssignment("new_event", str(uuid4()), 1),
    EventAssignment("existing_event", None, None),
])
def test_unbounded_or_malformed_assignment_never_writes(service, assignment):
    judgments = Judgments(assignment=assignment)
    assert outcome(ObservationPipeline(service, Extractor(), judgments))["status"] == "needs_review"
    assert counts(service)["evidence"] == 0


def test_wrong_version_and_real_cross_tenant_identity_rejected(service):
    target = seed_event(service)
    foreign = MemoryService(uuid4()); foreign.ensure_tenant()
    other_target = seed_event(foreign)
    for assignment in (EventAssignment("existing_event", str(target), 2), EventAssignment("existing_event", str(other_target), 1)):
        judgments = Judgments(assignment=assignment)
        assert outcome(ObservationPipeline(service, Extractor(), judgments))["status"] == "needs_review"
        assert str(other_target) not in [c["id"] for c in judgments.candidates]
    assert counts(service)["evidence"] == 1


def test_deleted_candidate_during_model_call_rejects_whole_capture(service):
    target = seed_event(service)
    judgments = Judgments(assignment=EventAssignment("existing_event", str(target), 1),
        on_assign=lambda candidates: service.delete_card(target))
    result = outcome(ObservationPipeline(service, Extractor(), judgments))
    assert result["reason"] == "candidate_snapshot_changed"
    assert judgments.calls == ["classify", "assign"]
    assert counts(service)["evidence"] == 1 and counts(service)["event_deltas"] == 0


def test_pending_delta_added_during_support_rejects_stale_decision(service):
    target = seed_event(service)
    judgments = Judgments(assignment=EventAssignment("existing_event", str(target), 1),
        on_support=lambda: service.add_event_delta(target, evidence("new independent progress"),
            {"current_result": "still waiting"}, idempotency_key="concurrent"))
    result = outcome(ObservationPipeline(service, Extractor(), judgments))
    assert result["reason"] == "candidate_snapshot_changed"
    assert counts(service)["evidence"] == 2 and counts(service)["event_deltas"] == 1


@pytest.mark.parametrize("support", ["unsupported", "contradicted", "uncertain", JudgmentFailure("offline_failure")])
def test_failed_support_has_no_partial_source_or_canonical_write(service, support):
    result = outcome(ObservationPipeline(service, Extractor(), Judgments(support=support)))
    assert result["status"] == "needs_review"
    assert counts(service) == {"evidence": 0, "semantic_cards": 0, "life_traces": 0, "event_deltas": 0}


@pytest.mark.parametrize("disposition,status", [("outside_scope", "outside_scope"), ("uncertain", "needs_review")])
def test_classification_nonwrite_routes(service, disposition, status):
    judgments = Judgments(disposition=disposition)
    assert outcome(ObservationPipeline(service, Extractor(), judgments))["status"] == status
    assert judgments.calls == ["classify"] and counts(service)["evidence"] == 0


def test_classification_mismatch_cannot_create_canonical_card(service):
    judgments = Judgments()
    judgments.classify_observation = lambda req, p: Classification("keep", "direct_statement", "user", "durable", "preference")
    assert outcome(ObservationPipeline(service, Extractor(), judgments))["reason"] == "classification_mismatch"
    assert counts(service)["semantic_cards"] == 0


@pytest.mark.parametrize("field,value", [
    ("subject", "unknown"), ("subject", "other"),
    ("speech_mode", "unknown"), ("speech_mode", "quotation"),
    ("persistence", "unknown"), ("persistence", "turn_only"),
])
def test_trace_classification_uncertainty_never_writes_source_or_trace(service, field, value):
    judgments = Judgments(disposition="trace_only")
    def classify(req, proposal):
        base = Classification("trace_only", "direct_statement", "user", "situational", "unknown")
        return replace(base, **{field: value})
    judgments.classify_observation = classify
    result = outcome(ObservationPipeline(service, Extractor(), judgments))
    assert result["status"] == "needs_review" and result["reason"] == "classification_mismatch"
    assert counts(service) == {"evidence": 0, "semantic_cards": 0, "life_traces": 0, "event_deltas": 0}
    assert "support" not in judgments.calls


def test_trace_allows_unknown_category_with_known_user_assertion_and_persistence(service):
    judgments = Judgments(disposition="trace_only")
    judgments.classify_observation = lambda req, proposal: Classification(
        "trace_only", "direct_statement", "user", "situational", "unknown")
    result = outcome(ObservationPipeline(service, Extractor(), judgments))
    assert result["status"] == "stored" and result["card_ids"] == []
    assert counts(service)["life_traces"] == 1


@pytest.mark.parametrize("classify_trace", [True, False])
def test_ordinary_trace_preserves_exact_source_without_forced_event(service, classify_trace):
    req = request("I sat by the window for a little while.")
    judgments = Judgments(disposition="trace_only" if classify_trace else "keep", assignment=EventAssignment("trace_only"))
    first = outcome(ObservationPipeline(service, Extractor(), judgments), req)
    assert first["status"] == "stored" and first["card_ids"] == []
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        assert conn.execute("SELECT observation FROM life_traces WHERE id=%s", (first["trace_id"],)).fetchone()["observation"] == req.content
    assert counts(service)["semantic_cards"] == 0


@pytest.mark.parametrize("card_type", ["fact", "prospective"])
def test_explicit_facts_and_future_commitments_keep_original_type(service, card_type):
    judgments = Judgments()
    first = outcome(ObservationPipeline(service, Extractor(card_type), judgments))
    assert first["status"] == "stored" and judgments.calls == ["classify", "support"]
    with tenant_transaction(service.tenant_id, mode="read") as conn:
        assert conn.execute("SELECT card_type FROM semantic_cards WHERE id=%s", (first["card_ids"][0],)).fetchone()["card_type"] == card_type


@pytest.mark.parametrize("metadata,reason", [({"sensitivity": "secret"}, "secret_source"),
    ({"authority": "third_party"}, "untrusted_subject_or_speaker")])
def test_private_or_nonauthoritative_source_not_sent_to_extractor(service, metadata, reason):
    extractor, judgments = Extractor(), Judgments()
    assert outcome(ObservationPipeline(service, extractor, judgments), request(**metadata))["reason"] == reason
    assert extractor.calls == 0 and judgments.calls == []


def test_sensitive_source_requires_host_grant_before_extract_and_save(service):
    req, extractor = request(sensitivity="sensitive"), Extractor()
    with pytest.raises(AuthorizationRequired):
        outcome(ObservationPipeline(service, extractor, Judgments()), req)
    assert extractor.calls == 0
    grants = []
    def grant(req, proposal):
        grants.append(proposal is not None)
        return True
    assert outcome(ObservationPipeline(service, extractor, Judgments(), authorize_sensitive=grant), req)["status"] == "stored"
    assert grants == [False, True]


@pytest.mark.parametrize("mutate,reason", [
    (lambda p: replace(p, speech_mode="quotation"), "non_authoritative_speech_mode"),
    (lambda p: replace(p, revision=RevisionRef("revise", "extractor-key")), "revision_required"),
    (lambda p: replace(p, privacy_class="never_store"), "never_store"),
])
def test_existing_gate_runs_before_any_judgment(service, mutate, reason):
    judgments = Judgments()
    assert outcome(ObservationPipeline(service, Extractor(mutate=mutate), judgments))["reason"] == reason
    assert judgments.calls == [] and counts(service)["evidence"] == 0


def test_source_replay_conflict_and_deleted_source_do_not_reextract(service):
    req, extractor = request(), Extractor()
    pipeline = ObservationPipeline(service, extractor, Judgments())
    first = outcome(pipeline, req)
    changed = ExtractionRequest.build(req.source.source_id, "1", "A different life claim.", req.occurred_at)
    with pytest.raises(IdempotencyConflict):
        outcome(pipeline, changed)
    service.delete_evidence(first["evidence_id"])
    with pytest.raises(DeletedSource):
        outcome(pipeline, req)
    assert extractor.calls == 1


def test_fts_retains_old_relevant_event_with_bounded_recent_candidates(service):
    old = seed_event(service, "orchard", "orchard trip planned")
    for index in range(6):
        seed_event(service, f"unrelated recent moment {index}")
    judgments = Judgments(assignment=EventAssignment("existing_event", str(old), 1))
    result = outcome(ObservationPipeline(service, Extractor(), judgments, candidate_limit=4), request("orchard"))
    assert result["status"] == "stored"
    assert str(old) in [c["id"] for c in judgments.candidates] and len(judgments.candidates) <= 4


def test_core_delta_expected_version_does_not_write_source(service):
    target = seed_event(service)
    with pytest.raises(VersionConflict):
        service.add_event_delta(target, evidence("stale progress"), {"current_result": "stale"},
            idempotency_key="stale", expected_version=2)
    assert counts(service)["evidence"] == 1 and counts(service)["event_deltas"] == 0


def test_unsafe_pending_candidate_requires_review_before_model_or_new_event(service):
    target = seed_event(service)
    service.add_event_delta(target, evidence("causal correction"), {"requires_restructure": True}, idempotency_key="unsafe")
    extractor, judgments = Extractor(), Judgments()
    result = outcome(ObservationPipeline(service, extractor, judgments))
    assert result["reason"] == "candidate_snapshot_unavailable"
    assert extractor.calls == 0 and judgments.calls == []
    assert counts(service)["evidence"] == 2 and counts(service)["semantic_cards"] == 1


@pytest.mark.parametrize("phase,expected_calls", [
    ("extractor", []), ("classify", ["classify"]), ("assignment", ["classify", "assign"]),
])
def test_source_deleted_during_external_phase_is_not_sent_to_next_phase(service, phase, expected_calls):
    original = service.observe(evidence("original moving source"), semantics=[event({
        "goal_context": "moving", "current_result": "waiting for help"})])
    target = original["card_ids"][0]
    delete = lambda: service.delete_evidence(original["evidence_id"])
    judgments = Judgments(assignment=EventAssignment("existing_event", str(target), 1))
    extractor = Extractor()
    if phase == "extractor":
        def after_extract(proposal):
            delete()
            return proposal
        extractor.mutate = after_extract
    elif phase == "classify":
        classify = judgments.classify_observation
        def after_classify(req, proposal):
            result = classify(req, proposal)
            delete()
            return result
        judgments.classify_observation = after_classify
    else:
        judgments.on_assign = lambda candidates: delete()
    result = outcome(ObservationPipeline(service, extractor, judgments))
    assert result["reason"] == "candidate_snapshot_changed"
    assert judgments.calls == expected_calls
    assert counts(service)["evidence"] == 1 and counts(service)["event_deltas"] == 0


def test_capture_source_identity_deleted_during_extraction_stops_judgments(service):
    from liveday0.local_extraction import GatedProposal, source_material
    req, judgments = request(), Judgments()
    def concurrently_capture_and_delete(proposal):
        source, _ = source_material(req, GatedProposal(proposal, None, False))
        receipt = service.observe(source)
        service.delete_evidence(receipt["evidence_id"])
        return proposal
    result = outcome(ObservationPipeline(service, Extractor(mutate=concurrently_capture_and_delete), judgments), req)
    assert result["reason"] == "source_snapshot_changed"
    assert judgments.calls == [] and counts(service)["semantic_cards"] == 0
