"""Closed, source-backed semantic extraction contract.

The objects in this module are deliberately independent from the extractor
adapter and from the persistence pipeline.  They describe an ephemeral
request and an extractor's proposed mapping, then provide deterministic
validation before a caller may hand a proposal to the existing memory core.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
import re
from typing import Any, ClassVar, Literal, Mapping

from liveday0.types import SemanticInput


__all__ = [
    "ContractViolation",
    "sha256_digest",
    "ExtractionSource",
    "ExtractionRequest",
    "SourceSpan",
    "ProposalSource",
    "SubjectRef",
    "ProposalScope",
    "ProducerMetadata",
    "RevisionRef",
    "ProposedSemanticInput",
    "SemanticProposal",
    "ProposalBatch",
    "ProposalValidation",
    "ProposalValidator",
]


REQUEST_SCHEMA_VERSION = "semantic-extraction-request.v1"
BATCH_SCHEMA_VERSION = "semantic-proposal-batch.v1"
PROPOSAL_SCHEMA_VERSION = "semantic-proposal.v1"

SourceKind = Literal["conversation", "document"]
SourceAuthority = Literal["user", "third_party", "unknown"]
SourceSensitivity = Literal["ordinary", "sensitive", "secret"]
Speaker = Literal["user", "assistant", "third_party", "document_author", "unknown"]
SubjectKind = Literal["user", "assistant", "named_person", "relationship", "unknown"]
SpeechMode = Literal[
    "direct_statement",
    "reported_statement",
    "quotation",
    "example",
    "hypothetical",
    "dynamic_placeholder",
    "tool_schema",
    "runtime_state",
]
SemanticCategory = Literal[
    "explicit_fact", "preference", "boundary", "event", "goal", "commitment"
]
Persistence = Literal["turn_only", "situational", "durable"]
TopEpistemicState = Literal["asserted", "inferred", "uncertain"]
PrivacyClass = Literal["ordinary", "sensitive", "never_store"]
PersistenceIntent = Literal["implicit_candidate", "explicit_save", "explicit_forget"]
RevisionIntent = Literal["create", "support", "revise", "counterevidence", "forget"]
CardType = Literal["event", "fact", "prospective"]
Lifecycle = Literal["active", "provisional"]
CardEpistemicState = Literal["candidate", "provisional", "confirmed"]


_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SEMVER_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_SOURCE_KINDS = frozenset({"conversation", "document"})
_AUTHORITIES = frozenset({"user", "third_party", "unknown"})
_SENSITIVITIES = frozenset({"ordinary", "sensitive", "secret"})
_SPEAKERS = frozenset({"user", "assistant", "third_party", "document_author", "unknown"})
_SUBJECT_KINDS = frozenset({"user", "assistant", "named_person", "relationship", "unknown"})
_SPEECH_MODES = frozenset(
    {
        "direct_statement",
        "reported_statement",
        "quotation",
        "example",
        "hypothetical",
        "dynamic_placeholder",
        "tool_schema",
        "runtime_state",
    }
)
_SEMANTIC_CATEGORIES = frozenset(
    {"explicit_fact", "preference", "boundary", "event", "goal", "commitment"}
)
_PERSISTENCE = frozenset({"turn_only", "situational", "durable"})
_TOP_EPISTEMIC_STATES = frozenset({"asserted", "inferred", "uncertain"})
_PRIVACY_CLASSES = frozenset({"ordinary", "sensitive", "never_store"})
_PERSISTENCE_INTENTS = frozenset({"implicit_candidate", "explicit_save", "explicit_forget"})
_REVISION_INTENTS = frozenset({"create", "support", "revise", "counterevidence", "forget"})
_CARD_TYPES = frozenset({"event", "fact", "prospective"})
_LIFECYCLES = frozenset({"active", "provisional"})
_CARD_EPISTEMIC_STATES = frozenset({"candidate", "provisional", "confirmed"})

_EVENT_BODY_REQUIRED = frozenset({"goal_context", "current_result"})
_EVENT_BODY_OPTIONAL = frozenset(
    {"development", "causal_turn", "unfinished_future", "boundaries", "uncertainty", "current"}
)
_FACT_BODY_REQUIRED = frozenset({"proposition", "scope"})
_FACT_BODY_OPTIONAL = frozenset({"boundaries", "applicability", "uncertainty", "safety_critical", "current"})
_PROSPECTIVE_BODY_REQUIRED = frozenset({"item", "status"})
_PROSPECTIVE_BODY_OPTIONAL = frozenset(
    {"trigger", "expected_window", "boundaries", "uncertainty", "current"}
)
_BODY_REQUIRED = {
    "event": _EVENT_BODY_REQUIRED,
    "fact": _FACT_BODY_REQUIRED,
    "prospective": _PROSPECTIVE_BODY_REQUIRED,
}
_BODY_OPTIONAL = {
    "event": _EVENT_BODY_OPTIONAL,
    "fact": _FACT_BODY_OPTIONAL,
    "prospective": _PROSPECTIVE_BODY_OPTIONAL,
}
_BODY_BOOLEAN_FIELDS = frozenset({"current", "safety_critical"})


class ContractViolation(ValueError):
    """Raised when an input cannot be represented by the closed contract."""

    def __init__(self, message: str, *, code: str = "contract_violation", path: str | None = None):
        self.code = code
        self.path = path
        detail = f"{path}: {message}" if path else message
        super().__init__(detail)


def sha256_digest(value: str | bytes) -> str:
    """Return the contract digest form, ``sha256:<64 lowercase hex>``."""

    if isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, bytes):
        raw = value
    else:
        raise TypeError("sha256_digest expects str or bytes")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _fail(message: str, *, path: str, code: str = "contract_violation") -> None:
    raise ContractViolation(message, path=path, code=code)


def _mapping(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("expected an object", path=path, code="invalid_type")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str] | frozenset[str], *, path: str) -> None:
    actual = set(value.keys())
    unknown = sorted(actual - set(expected))
    missing = sorted(set(expected) - actual)
    if unknown:
        _fail(f"unknown field(s): {', '.join(unknown)}", path=path, code="unknown_field")
    if missing:
        _fail(f"missing field(s): {', '.join(missing)}", path=path, code="missing_field")


def _string(value: Any, *, path: str, maximum: int = 2_000, identifier: bool = False) -> str:
    if not isinstance(value, str):
        _fail("expected a string", path=path, code="invalid_type")
    if not value or not value.strip():
        _fail("must be non-empty", path=path, code="invalid_value")
    if len(value) > maximum:
        _fail(f"must be at most {maximum} Unicode code points", path=path, code="invalid_value")
    if identifier and not _SOURCE_ID_RE.fullmatch(value):
        _fail("must match [A-Za-z0-9._:-] and be at most 128 characters", path=path, code="invalid_value")
    return value


def _integer(value: Any, *, path: str, minimum: int | None = None, maximum: int | None = None) -> int:
    if type(value) is not int:
        _fail("expected an integer", path=path, code="invalid_type")
    if minimum is not None and value < minimum:
        _fail(f"must be >= {minimum}", path=path, code="invalid_value")
    if maximum is not None and value > maximum:
        _fail(f"must be <= {maximum}", path=path, code="invalid_value")
    return value


def _enum(value: Any, choices: frozenset[str], *, path: str) -> str:
    if not isinstance(value, str):
        _fail("expected a string enum value", path=path, code="invalid_type")
    if value not in choices:
        _fail(f"unsupported value {value!r}", path=path, code="invalid_value")
    return value


def _digest(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        _fail("expected a digest string", path=path, code="invalid_type")
    if not _DIGEST_RE.fullmatch(value):
        _fail("expected sha256:<64 lowercase hex>", path=path, code="invalid_value")
    return value


def _timestamp(value: Any, *, path: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value
        if not text or not text.strip():
            _fail("must be non-empty", path=path, code="invalid_value")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractViolation("invalid RFC 3339 timestamp", path=path, code="invalid_value") from exc
    else:
        _fail("expected an RFC 3339 timestamp", path=path, code="invalid_type")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("timestamp must include a timezone", path=path, code="invalid_value")
    return parsed.astimezone(timezone.utc)


def _timestamp_from_dict(value: Any, *, path: str) -> str:
    """Require the wire representation to be an RFC 3339 string."""

    if not isinstance(value, str):
        _fail("expected an RFC 3339 timestamp string", path=path, code="invalid_type")
    _timestamp(value, path=path)
    return value


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _schema(value: Any, expected: str, *, path: str) -> str:
    if not isinstance(value, str) or value != expected:
        _fail(f"expected schema version {expected!r}", path=path, code="invalid_schema_version")
    return value


def _validate_body(card_type: Any, body: Any, *, path: str) -> None:
    if not isinstance(card_type, str) or card_type not in _CARD_TYPES:
        _fail("unsupported card type", path=f"{path}.card_type", code="invalid_value")
    if not isinstance(body, Mapping):
        _fail("expected an object", path=path, code="invalid_type")
    required = _BODY_REQUIRED[card_type]
    optional = _BODY_OPTIONAL[card_type]
    unknown = sorted(set(body.keys()) - set(required | optional))
    if unknown:
        _fail(f"unknown field(s): {', '.join(unknown)}", path=path, code="unknown_field")
    missing = required - set(body.keys())
    if missing:
        _fail(f"missing field(s): {', '.join(sorted(missing))}", path=path, code="missing_field")
    for key, value in body.items():
        key_path = f"{path}.{key}"
        if key in _BODY_BOOLEAN_FIELDS:
            if type(value) is not bool:
                _fail("expected a boolean", path=key_path, code="invalid_type")
        else:
            _string(value, path=key_path)


def _body_is_valid(card_type: Any, body: Any) -> bool:
    try:
        _validate_body(card_type, body, path="semantic_input.body")
    except ContractViolation:
        return False
    return True


@dataclass(frozen=True)
class ExtractionSource:
    source_id: str
    source_version: str
    source_kind: SourceKind
    authority: SourceAuthority
    sensitivity: SourceSensitivity
    chunk_start: int
    chunk_end: int
    content_digest: str

    def __post_init__(self) -> None:
        _string(self.source_id, path="source.source_id", maximum=128, identifier=True)
        _string(self.source_version, path="source.source_version", maximum=128, identifier=True)
        _enum(self.source_kind, _SOURCE_KINDS, path="source.source_kind")
        _enum(self.authority, _AUTHORITIES, path="source.authority")
        _enum(self.sensitivity, _SENSITIVITIES, path="source.sensitivity")
        _integer(self.chunk_start, path="source.chunk_start", minimum=0)
        _integer(self.chunk_end, path="source.chunk_end", minimum=0)
        if self.chunk_end - self.chunk_start < 0:
            _fail("must be >= chunk_start", path="source.chunk_end", code="invalid_value")
        _digest(self.content_digest, path="source.content_digest")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExtractionSource":
        data = _mapping(value, path="source")
        _exact_keys(
            data,
            {"source_id", "source_version", "source_kind", "authority", "sensitivity", "chunk_start", "chunk_end", "content_digest"},
            path="source",
        )
        return cls(
            source_id=data["source_id"],
            source_version=data["source_version"],
            source_kind=data["source_kind"],
            authority=data["authority"],
            sensitivity=data["sensitivity"],
            chunk_start=data["chunk_start"],
            chunk_end=data["chunk_end"],
            content_digest=data["content_digest"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "source_kind": self.source_kind,
            "authority": self.authority,
            "sensitivity": self.sensitivity,
            "chunk_start": self.chunk_start,
            "chunk_end": self.chunk_end,
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True)
class ExtractionRequest:
    source: ExtractionSource
    content: str
    occurred_at: datetime
    max_proposals: int
    schema_version: str = REQUEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version, REQUEST_SCHEMA_VERSION, path="schema_version")
        if not isinstance(self.source, ExtractionSource):
            _fail("expected ExtractionSource", path="source", code="invalid_type")
        _string(self.content, path="content", maximum=12_000)
        if len(self.content) != self.source.chunk_end - self.source.chunk_start:
            _fail("chunk_end - chunk_start must equal len(content)", path="source", code="invalid_value")
        expected_digest = sha256_digest(self.content)
        if self.source.content_digest != expected_digest:
            _fail("does not match UTF-8 content digest", path="source.content_digest", code="invalid_value")
        normalized = _timestamp(self.occurred_at, path="occurred_at")
        object.__setattr__(self, "occurred_at", normalized)
        _integer(self.max_proposals, path="max_proposals", minimum=1, maximum=16)

    @classmethod
    def build(
        cls,
        source_id: str,
        source_version: str,
        content: str,
        occurred_at: datetime | str,
        *,
        source_kind: SourceKind = "conversation",
        authority: SourceAuthority = "user",
        sensitivity: SourceSensitivity = "ordinary",
        chunk_start: int = 0,
        max_proposals: int = 8,
    ) -> "ExtractionRequest":
        """Build a request while deriving its end offset and content digest."""

        _string(content, path="content", maximum=12_000)
        _integer(chunk_start, path="source.chunk_start", minimum=0)
        source = ExtractionSource(
            source_id=source_id,
            source_version=source_version,
            source_kind=source_kind,
            authority=authority,
            sensitivity=sensitivity,
            chunk_start=chunk_start,
            chunk_end=chunk_start + len(content),
            content_digest=sha256_digest(content),
        )
        return cls(source=source, content=content, occurred_at=occurred_at, max_proposals=max_proposals)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExtractionRequest":
        data = _mapping(value, path="request")
        _exact_keys(data, {"schema_version", "source", "content", "occurred_at", "max_proposals"}, path="request")
        return cls(
            source=ExtractionSource.from_dict(data["source"]),
            content=data["content"],
            occurred_at=_timestamp_from_dict(data["occurred_at"], path="request.occurred_at"),
            max_proposals=data["max_proposals"],
            schema_version=data["schema_version"],
        )

    @property
    def chunk_start(self) -> int:
        return self.source.chunk_start

    @property
    def chunk_end(self) -> int:
        return self.source.chunk_end

    @property
    def content_digest(self) -> str:
        return self.source.content_digest

    @property
    def fingerprint(self) -> str:
        parts = (
            self.source.source_id,
            self.source.source_version,
            str(self.source.chunk_start),
            str(self.source.chunk_end),
            self.source.content_digest,
            _timestamp_text(self.occurred_at),
            str(self.max_proposals),
        )
        return sha256_digest("\0".join(parts))

    @property
    def request_fingerprint(self) -> str:
        return self.fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source.to_dict(),
            "content": self.content,
            "occurred_at": _timestamp_text(self.occurred_at),
            "max_proposals": self.max_proposals,
        }


@dataclass(frozen=True)
class SourceSpan:
    start: int
    end: int
    digest: str

    def __post_init__(self) -> None:
        _integer(self.start, path="span.start", minimum=0)
        _integer(self.end, path="span.end", minimum=0)
        if self.end <= self.start:
            _fail("must be greater than start", path="span.end", code="invalid_value")
        _digest(self.digest, path="span.digest")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceSpan":
        data = _mapping(value, path="source.span")
        _exact_keys(data, {"start", "end", "digest"}, path="source.span")
        return cls(start=data["start"], end=data["end"], digest=data["digest"])

    @classmethod
    def from_text(cls, start: int, text: str) -> "SourceSpan":
        _integer(start, path="span.start", minimum=0)
        _string(text, path="span_text")
        return cls(start=start, end=start + len(text), digest=sha256_digest(text))

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "digest": self.digest}


@dataclass(frozen=True)
class ProposalSource:
    source_id: str
    source_version: str
    content_digest: str
    span: SourceSpan

    def __post_init__(self) -> None:
        _string(self.source_id, path="proposal.source.source_id", maximum=128, identifier=True)
        _string(self.source_version, path="proposal.source.source_version", maximum=128, identifier=True)
        _digest(self.content_digest, path="proposal.source.content_digest")
        if not isinstance(self.span, SourceSpan):
            _fail("expected SourceSpan", path="proposal.source.span", code="invalid_type")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProposalSource":
        data = _mapping(value, path="proposal.source")
        _exact_keys(data, {"source_id", "source_version", "content_digest", "span"}, path="proposal.source")
        return cls(
            source_id=data["source_id"],
            source_version=data["source_version"],
            content_digest=data["content_digest"],
            span=SourceSpan.from_dict(data["span"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "content_digest": self.content_digest,
            "span": self.span.to_dict(),
        }


@dataclass(frozen=True)
class SubjectRef:
    kind: SubjectKind
    identifier: str

    def __post_init__(self) -> None:
        _enum(self.kind, _SUBJECT_KINDS, path="subject.kind")
        _string(self.identifier, path="subject.identifier")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubjectRef":
        data = _mapping(value, path="subject")
        _exact_keys(data, {"kind", "identifier"}, path="subject")
        return cls(kind=data["kind"], identifier=data["identifier"])

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "identifier": self.identifier}


@dataclass(frozen=True)
class ProposalScope:
    persistence: Persistence
    applies_to: str

    def __post_init__(self) -> None:
        _enum(self.persistence, _PERSISTENCE, path="scope.persistence")
        _string(self.applies_to, path="scope.applies_to")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProposalScope":
        data = _mapping(value, path="scope")
        _exact_keys(data, {"persistence", "applies_to"}, path="scope")
        return cls(persistence=data["persistence"], applies_to=data["applies_to"])

    def to_dict(self) -> dict[str, Any]:
        return {"persistence": self.persistence, "applies_to": self.applies_to}


@dataclass(frozen=True)
class ProducerMetadata:
    producer_id: str
    model_id: str
    extractor_version: str
    policy_version: str

    def __post_init__(self) -> None:
        _string(self.producer_id, path="producer.producer_id")
        _string(self.model_id, path="producer.model_id")
        _string(self.extractor_version, path="producer.extractor_version")
        _string(self.policy_version, path="producer.policy_version")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProducerMetadata":
        data = _mapping(value, path="producer")
        _exact_keys(data, {"producer_id", "model_id", "extractor_version", "policy_version"}, path="producer")
        return cls(
            producer_id=data["producer_id"],
            model_id=data["model_id"],
            extractor_version=data["extractor_version"],
            policy_version=data["policy_version"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "producer_id": self.producer_id,
            "model_id": self.model_id,
            "extractor_version": self.extractor_version,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class RevisionRef:
    intent: RevisionIntent
    key: str

    def __post_init__(self) -> None:
        _enum(self.intent, _REVISION_INTENTS, path="revision.intent")
        _string(self.key, path="revision.key")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RevisionRef":
        data = _mapping(value, path="revision")
        _exact_keys(data, {"intent", "key"}, path="revision")
        return cls(intent=data["intent"], key=data["key"])

    def to_dict(self) -> dict[str, Any]:
        return {"intent": self.intent, "key": self.key}


@dataclass(frozen=True)
class ProposedSemanticInput:
    card_type: CardType
    body: dict[str, Any]
    lifecycle: Lifecycle
    epistemic_state: CardEpistemicState
    canonical_key: str
    valid_at: datetime

    def __post_init__(self) -> None:
        _enum(self.card_type, _CARD_TYPES, path="semantic_input.card_type")
        _validate_body(self.card_type, self.body, path="semantic_input.body")
        _enum(self.lifecycle, _LIFECYCLES, path="semantic_input.lifecycle")
        _enum(self.epistemic_state, _CARD_EPISTEMIC_STATES, path="semantic_input.epistemic_state")
        _string(self.canonical_key, path="semantic_input.canonical_key")
        normalized = _timestamp(self.valid_at, path="semantic_input.valid_at")
        object.__setattr__(self, "valid_at", normalized)
        object.__setattr__(self, "body", dict(self.body))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProposedSemanticInput":
        data = _mapping(value, path="semantic_input")
        _exact_keys(
            data,
            {"card_type", "body", "lifecycle", "epistemic_state", "canonical_key", "valid_at"},
            path="semantic_input",
        )
        return cls(
            card_type=data["card_type"],
            body=data["body"],
            lifecycle=data["lifecycle"],
            epistemic_state=data["epistemic_state"],
            canonical_key=data["canonical_key"],
            valid_at=_timestamp_from_dict(data["valid_at"], path="semantic_input.valid_at"),
        )

    def to_semantic_input(self) -> SemanticInput:
        """Convert the proposed mapping to the existing core input type."""

        return SemanticInput(
            card_type=self.card_type,
            body=dict(self.body),
            lifecycle=self.lifecycle,
            epistemic_state=self.epistemic_state,
            canonical_key=self.canonical_key,
            valid_at=self.valid_at,
        )

    # A descriptive alias is useful to callers while retaining one conversion
    # implementation and one canonical core type.
    as_semantic_input = to_semantic_input

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_type": self.card_type,
            "body": dict(self.body),
            "lifecycle": self.lifecycle,
            "epistemic_state": self.epistemic_state,
            "canonical_key": self.canonical_key,
            "valid_at": _timestamp_text(self.valid_at),
        }


@dataclass(frozen=True)
class SemanticProposal:
    proposal_id: str
    source: ProposalSource
    speaker: Speaker
    subject: SubjectRef
    speech_mode: SpeechMode
    semantic_category: SemanticCategory
    scope: ProposalScope
    confidence: float
    epistemic_state: TopEpistemicState
    privacy_class: PrivacyClass
    persistence_intent: PersistenceIntent
    revision: RevisionRef
    producer: ProducerMetadata
    semantic_input: ProposedSemanticInput | None
    schema_version: str = PROPOSAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version, PROPOSAL_SCHEMA_VERSION, path="proposal.schema_version")
        _string(self.proposal_id, path="proposal.proposal_id")
        if not isinstance(self.source, ProposalSource):
            _fail("expected ProposalSource", path="proposal.source", code="invalid_type")
        _enum(self.speaker, _SPEAKERS, path="proposal.speaker")
        if not isinstance(self.subject, SubjectRef):
            _fail("expected SubjectRef", path="proposal.subject", code="invalid_type")
        _enum(self.speech_mode, _SPEECH_MODES, path="proposal.speech_mode")
        _enum(self.semantic_category, _SEMANTIC_CATEGORIES, path="proposal.semantic_category")
        if not isinstance(self.scope, ProposalScope):
            _fail("expected ProposalScope", path="proposal.scope", code="invalid_type")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            _fail("expected a finite number", path="proposal.confidence", code="invalid_type")
        if not math.isfinite(float(self.confidence)) or not 0 <= float(self.confidence) <= 1:
            _fail("must be finite and within [0, 1]", path="proposal.confidence", code="invalid_value")
        _enum(self.epistemic_state, _TOP_EPISTEMIC_STATES, path="proposal.epistemic_state")
        _enum(self.privacy_class, _PRIVACY_CLASSES, path="proposal.privacy_class")
        _enum(self.persistence_intent, _PERSISTENCE_INTENTS, path="proposal.persistence_intent")
        if not isinstance(self.revision, RevisionRef):
            _fail("expected RevisionRef", path="proposal.revision", code="invalid_type")
        if not isinstance(self.producer, ProducerMetadata):
            _fail("expected ProducerMetadata", path="proposal.producer", code="invalid_type")
        if self.semantic_input is not None and not isinstance(self.semantic_input, ProposedSemanticInput):
            _fail("expected ProposedSemanticInput or null", path="proposal.semantic_input", code="invalid_type")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SemanticProposal":
        data = _mapping(value, path="proposal")
        _exact_keys(
            data,
            {
                "schema_version",
                "proposal_id",
                "source",
                "speaker",
                "subject",
                "speech_mode",
                "semantic_category",
                "scope",
                "confidence",
                "epistemic_state",
                "privacy_class",
                "persistence_intent",
                "revision",
                "producer",
                "semantic_input",
            },
            path="proposal",
        )
        semantic_input = None if data["semantic_input"] is None else ProposedSemanticInput.from_dict(data["semantic_input"])
        return cls(
            schema_version=data["schema_version"],
            proposal_id=data["proposal_id"],
            source=ProposalSource.from_dict(data["source"]),
            speaker=data["speaker"],
            subject=SubjectRef.from_dict(data["subject"]),
            speech_mode=data["speech_mode"],
            semantic_category=data["semantic_category"],
            scope=ProposalScope.from_dict(data["scope"]),
            confidence=data["confidence"],
            epistemic_state=data["epistemic_state"],
            privacy_class=data["privacy_class"],
            persistence_intent=data["persistence_intent"],
            revision=RevisionRef.from_dict(data["revision"]),
            producer=ProducerMetadata.from_dict(data["producer"]),
            semantic_input=semantic_input,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "source": self.source.to_dict(),
            "speaker": self.speaker,
            "subject": self.subject.to_dict(),
            "speech_mode": self.speech_mode,
            "semantic_category": self.semantic_category,
            "scope": self.scope.to_dict(),
            "confidence": self.confidence,
            "epistemic_state": self.epistemic_state,
            "privacy_class": self.privacy_class,
            "persistence_intent": self.persistence_intent,
            "revision": self.revision.to_dict(),
            "producer": self.producer.to_dict(),
            "semantic_input": None if self.semantic_input is None else self.semantic_input.to_dict(),
        }


@dataclass(frozen=True)
class ProposalBatch:
    request_fingerprint: str
    proposals: tuple[SemanticProposal, ...]
    schema_version: str = BATCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _schema(self.schema_version, BATCH_SCHEMA_VERSION, path="batch.schema_version")
        _digest(self.request_fingerprint, path="batch.request_fingerprint")
        if not isinstance(self.proposals, (list, tuple)):
            _fail("expected an array", path="batch.proposals", code="invalid_type")
        proposals = tuple(self.proposals)
        for index, proposal in enumerate(proposals):
            if not isinstance(proposal, SemanticProposal):
                _fail(f"expected SemanticProposal at index {index}", path="batch.proposals", code="invalid_type")
        object.__setattr__(self, "proposals", proposals)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProposalBatch":
        data = _mapping(value, path="batch")
        _exact_keys(data, {"schema_version", "request_fingerprint", "proposals"}, path="batch")
        if not isinstance(data["proposals"], list):
            _fail("expected an array", path="batch.proposals", code="invalid_type")
        return cls(
            schema_version=data["schema_version"],
            request_fingerprint=data["request_fingerprint"],
            proposals=tuple(SemanticProposal.from_dict(item) for item in data["proposals"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_fingerprint": self.request_fingerprint,
            "proposals": [proposal.to_dict() for proposal in self.proposals],
        }


@dataclass(frozen=True)
class ProposalValidation:
    proposal_id: str
    valid: bool
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _string(self.proposal_id, path="validation.proposal_id")
        if type(self.valid) is not bool:
            _fail("expected a boolean", path="validation.valid", code="invalid_type")
        if not isinstance(self.reason_codes, (list, tuple)):
            _fail("expected an array", path="validation.reason_codes", code="invalid_type")
        codes = tuple(self.reason_codes)
        for code in codes:
            _string(code, path="validation.reason_codes", maximum=128)
        if self.valid != (not codes):
            _fail("valid must agree with reason_codes", path="validation", code="invalid_value")
        object.__setattr__(self, "reason_codes", codes)

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.reason_codes

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "valid": self.valid,
            "reason_codes": list(self.reason_codes),
        }


class ProposalValidator:
    """Independent deterministic validator for source-backed proposals."""

    CATEGORY_CARD_TYPES: ClassVar[dict[str, str]] = {
        "explicit_fact": "fact",
        "preference": "fact",
        "boundary": "fact",
        "event": "event",
        "goal": "prospective",
        "commitment": "prospective",
    }

    @staticmethod
    def validate(request: ExtractionRequest, batch: ProposalBatch) -> list[ProposalValidation]:
        if not isinstance(request, ExtractionRequest):
            raise ContractViolation("expected ExtractionRequest", path="request", code="invalid_type")
        if not isinstance(batch, ProposalBatch):
            raise ContractViolation("expected ProposalBatch", path="batch", code="invalid_type")

        fingerprint_mismatch = batch.request_fingerprint != request.fingerprint
        batch_size_exceeded = len(batch.proposals) > request.max_proposals

        proposal_id_counts: dict[str, int] = {}
        span_counts: dict[tuple[Any, Any], int] = {}
        for proposal in batch.proposals:
            proposal_id_counts[proposal.proposal_id] = proposal_id_counts.get(proposal.proposal_id, 0) + 1
            span = proposal.source.span
            key = (span.start, span.end)
            span_counts[key] = span_counts.get(key, 0) + 1

        results: list[ProposalValidation] = []
        for proposal in batch.proposals:
            reasons: list[str] = []
            if fingerprint_mismatch:
                reasons.append("request_fingerprint_mismatch")
            if batch_size_exceeded:
                reasons.append("batch_size_exceeded")
            if proposal_id_counts.get(proposal.proposal_id, 0) > 1:
                reasons.append("duplicate_proposal_id")
            span = proposal.source.span
            if span_counts.get((span.start, span.end), 0) > 1:
                reasons.append("duplicate_span")

            source = request.source
            span_out_of_bounds = not (
                span.start >= source.chunk_start
                and span.end <= source.chunk_end
                and span.end > span.start
            )
            if span_out_of_bounds:
                reasons.append("span_out_of_bounds")

            if proposal.source.source_id != source.source_id or proposal.source.source_version != source.source_version:
                reasons.append("source_identity_mismatch")
            if proposal.source.content_digest != source.content_digest or source.content_digest != sha256_digest(request.content):
                reasons.append("source_digest_mismatch")
            if not span_out_of_bounds:
                relative_start = span.start - source.chunk_start
                relative_end = span.end - source.chunk_start
                actual_span_digest = sha256_digest(request.content[relative_start:relative_end])
                if proposal.source.span.digest != actual_span_digest:
                    reasons.append("span_digest_mismatch")

            if not isinstance(proposal.speaker, str) or not proposal.speaker.strip():
                reasons.append("invalid_speaker")
            if (
                not isinstance(proposal.subject, SubjectRef)
                or not isinstance(proposal.subject.identifier, str)
                or not proposal.subject.identifier.strip()
            ):
                reasons.append("invalid_subject")
            if (
                not isinstance(proposal.scope, ProposalScope)
                or not isinstance(proposal.scope.applies_to, str)
                or not proposal.scope.applies_to.strip()
            ):
                reasons.append("invalid_scope")

            if not isinstance(proposal.producer.extractor_version, str) or not _SEMVER_RE.fullmatch(
                proposal.producer.extractor_version
            ):
                reasons.append("invalid_extractor_version")
            if not isinstance(proposal.producer.policy_version, str) or not _SEMVER_RE.fullmatch(
                proposal.producer.policy_version
            ):
                reasons.append("invalid_policy_version")

            semantic = proposal.semantic_input
            if semantic is not None:
                expected_card_type = ProposalValidator.CATEGORY_CARD_TYPES.get(proposal.semantic_category)
                if expected_card_type != semantic.card_type:
                    reasons.append("category_card_type_mismatch")
                if semantic.lifecycle not in _LIFECYCLES:
                    reasons.append("invalid_lifecycle")
                if semantic.epistemic_state not in _CARD_EPISTEMIC_STATES:
                    reasons.append("invalid_semantic_epistemic_state")
                if semantic.canonical_key != proposal.revision.key:
                    reasons.append("revision_key_mismatch")
                if not _body_is_valid(semantic.card_type, semantic.body):
                    reasons.append("invalid_semantic_body")

            forget_shape = (proposal.revision.intent == "forget" or proposal.persistence_intent == "explicit_forget")
            if forget_shape != (semantic is None):
                reasons.append("invalid_forget_shape")

            results.append(
                ProposalValidation(
                    proposal_id=proposal.proposal_id,
                    valid=not reasons,
                    reason_codes=tuple(reasons),
                )
            )
        return results
