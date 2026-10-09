"""Small synthetic adapter and gated seam; no provider calls or persistence.

Closed schema/validator comes from the separately archived LE contract. Source
and span digests are ephemeral validation data, never durable locators.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, NAMESPACE_URL, uuid5

from liveday0.extraction_contract import (
    ExtractionRequest, ProposalBatch, ProposalValidator, SemanticProposal,
    ProposalSource, SourceSpan, SubjectRef, ProposalScope, RevisionRef,
    ProducerMetadata, ProposedSemanticInput,
)
from liveday0.serialization import canonical_json
from liveday0.types import EvidenceInput


SYNTHETIC_EXAMPLES = {
    "合成生活：周末搬家，等待朋友来帮忙。": {
        "goal_context":"合成搬家", "current_result":"等待朋友帮忙", "current":True},
    "合成生活：朋友已来帮忙，搬家完成。": {
        "goal_context":"合成搬家", "current_result":"搬家完成", "current":True},
    "合成生活：我决定每周约朋友吃饭。": {
        "goal_context":"合成朋友往来", "current_result":"决定每周约朋友吃饭", "unfinished_future":"等待实行"},
}
PRODUCER = ProducerMetadata("local-synthetic", "deterministic-fixtures", "1.0.0", "1.0.0")


class DeterministicLocalAdapter:
    """Exact fixture matching only. Unknown text returns no proposals."""
    production_ready = False

    def extract(self, request):
        proposals=[]
        for index,(text,body) in enumerate(SYNTHETIC_EXAMPLES.items()):
            if request.content != text:
                continue
            start=0
            absolute=request.source.chunk_start+start
            key=f"synthetic:{request.source.source_id}:{absolute}"
            proposals.append(SemanticProposal(
                proposal_id=f"fixture-{index}",
                source=ProposalSource(request.source.source_id,request.source.source_version,
                    request.content_digest,SourceSpan.from_text(absolute,text)),
                speaker="user",subject=SubjectRef("user","current-local-user"),
                speech_mode="direct_statement",semantic_category="event",
                scope=ProposalScope("durable","synthetic user"),confidence=1.0,
                epistemic_state="asserted",privacy_class="ordinary",
                persistence_intent="implicit_candidate",revision=RevisionRef("create",key),
                producer=PRODUCER,semantic_input=ProposedSemanticInput(
                    "event",deepcopy(body),"active","confirmed",key,request.occurred_at)))
        return ProposalBatch(request.fingerprint,tuple(proposals))


@dataclass(frozen=True)
class GatedProposal:
    proposal: SemanticProposal | None
    reason: str | None
    needs_confirmation: bool


def validate_and_gate(request, adapter):
    """Decode a closed bounded batch before any MemoryService call.

    A proposal's explicit_save flag is never proof of human authorization.
    Sensitive material requires the separate host confirmation channel.
    """
    if request.source.sensitivity=='secret':
        return [GatedProposal(None,'secret_source',False)]
    value=adapter.extract(deepcopy(request))
    value=deepcopy(value.to_dict() if isinstance(value,ProposalBatch) else value)
    if len(canonical_json(value).encode('utf-8')) > 64_000:
        raise ValueError("proposal batch exceeds bounded output")
    batch=ProposalBatch.from_dict(value)
    validations=ProposalValidator.validate(request,batch)
    never=[p.source.span for p,v in zip(batch.proposals,validations)
           if v.valid and p.privacy_class=='never_store']
    results=[]
    for p,v in zip(batch.proposals,validations):
        reason=None
        if not v.valid: reason=v.reason_codes[0]
        elif p.privacy_class=='never_store': reason='never_store'
        elif any(p.source.span.start < s.end and s.start < p.source.span.end for s in never): reason='overlaps_never_store'
        elif p.speech_mode!='direct_statement': reason='non_authoritative_speech_mode'
        elif p.subject.kind!='user' or p.speaker!='user' or request.source.authority!='user': reason='untrusted_subject_or_speaker'
        elif p.epistemic_state!='asserted': reason='not_asserted'
        elif p.scope.persistence=='turn_only': reason='turn_only'
        elif p.revision.intent=='forget' or p.persistence_intent=='explicit_forget': reason='forget_required'
        elif p.revision.intent!='create': reason='revision_required'
        sensitive=request.source.sensitivity=='sensitive' or p.privacy_class=='sensitive'
        results.append(GatedProposal(p,reason,sensitive))
    return results


def source_material(request, gated):
    p=gated.proposal;span=p.source.span
    # UUID source identity is supplied by the local host, never derived from content.
    UUID(request.source.source_id)
    locator={'schema':'local-source.v1','message_id':request.source.source_id,
        'version':request.source.source_version,'start':span.start,'end':span.end,
        'sensitivity':'sensitive' if gated.needs_confirmation else 'ordinary'}
    # This narrow entry permits one accepted span per message/version. Changing
    # segmentation cannot allocate a new evidence identity or bypass revocation.
    key=str(uuid5(NAMESPACE_URL,canonical_json(['liveday0:local-source:v1',
        locator['message_id'],locator['version']])))
    evidence=EvidenceInput('text','local_user_session',
        request.content[span.start-request.chunk_start:span.end-request.chunk_start],
        object_ref=canonical_json(locator),occurred_at=request.occurred_at,idempotency_key=key)
    # Canonical identity is host allocated; model prose/key cannot become a locator.
    semantic=replace(p.semantic_input.to_semantic_input(),canonical_key=f'local:{key}')
    return evidence,semantic


def saved_request(prepared):
    """Reconstruct only the accepted saved span; never request its surroundings."""
    import json
    source=prepared.payload['source'];loc=json.loads(source['object_ref'] or '{}')
    expected={'schema','message_id','version','start','end','sensitivity'}
    if set(loc)!=expected or loc['schema']!='local-source.v1':
        raise ValueError('source has no trusted local locator; no legacy guessing')
    UUID(loc['message_id'])
    if type(loc['start']) is not int or type(loc['end']) is not int or loc['end'] != loc['start']+len(source['content']):
        raise ValueError('saved span does not match the local locator')
    return ExtractionRequest.build(loc['message_id'],loc['version'],source['content'],
        datetime.fromisoformat(source['occurred_at']),chunk_start=loc['start'],
        sensitivity=loc['sensitivity'],authority='user',source_kind='conversation')
