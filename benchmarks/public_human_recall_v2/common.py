from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DATASET_PATH = ROOT / "dataset.jsonl"
SOURCES_PATH = ROOT / "sources.json"
AUDIT_PATH = ROOT / "source_audit.json"

ALLOWED_LANGUAGES = {"zh", "en", "es"}
ALLOWED_LICENSES = {"CC BY-SA 4.0"}
ALLOWED_FAMILIES = {
    "wikimedia",
    "fedora_discussion",
    "zcash_forum",
    "stackexchange_v1_frozen",
}
ALLOWED_HOSTS: dict[str, tuple[str, ...]] = {
    "wikimedia": ("wikibooks.org", "wikinews.org", "wikiquote.org", "wikivoyage.org", "wiktionary.org"),
    "fedora_discussion": ("discussion.fedoraproject.org",),
    "zcash_forum": ("forum.zcashcommunity.com",),
    "stackexchange_v1_frozen": (
        "workplace.stackexchange.com", "bicycles.stackexchange.com", "cooking.stackexchange.com",
    ),
}

CONTACT_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){7,}\d(?!\d)"),
    re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
)
HIGH_SENSITIVITY_PATTERNS = (
    re.compile(
        r"\b(?:diagnos\w*|disease|cancer|pregnan\w*|miscarriage|suicid\w*|"
        r"mental health|medical record|bank account|credit card|wallet address|"
        r"seed phrase|private key|salary|income|debt|mortgage|street address|"
        r"passport|social security|government id)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:诊断|疾病|癌症|怀孕|流产|自杀|心理健康|病历|银行卡|信用卡|钱包地址|私钥|助记词|工资|收入|债务|房贷|住址|身份证)"),
    re.compile(r"(?:diagnóstico|enfermedad|cáncer|embarazo|suicidio|salud mental|cuenta bancaria|tarjeta de crédito|dirección exacta)", re.IGNORECASE),
)
MINOR_PATTERNS = (
    re.compile(r"\b(?:minor|underage|child|children|kid|teenager|schoolchild)\b", re.IGNORECASE),
    re.compile(r"(?:未成年|儿童|小孩|孩子|中学生|小学生)"),
    re.compile(r"\b(?:menor de edad|niñ[oa]s?|adolescente)\b", re.IGNORECASE),
)

DOMAIN_PATTERNS: dict[str, tuple[str, ...]] = {
    "work": ("work", "job", "career", "team", "工作", "职业", "trabajo", "equipo"),
    "learning": ("learn", "study", "understand", "学习", "了解", "aprender", "estudiar"),
    "task_project": ("task", "project", "build", "implement", "任务", "项目", "proyecto", "tarea"),
    "change_transition": ("change", "upgrade", "move", "became", "变化", "升级", "cambiar", "actualizar"),
    "planning_future": ("plan", "next", "will", "hope", "计划", "以后", "下一步", "futuro", "planeo"),
    "decision_preference": ("decide", "prefer", "choose", "option", "决定", "更喜欢", "选择", "prefiero", "elegir"),
    "routine_habit": ("daily", "usually", "routine", "often", "每天", "经常", "日常", "diario", "normalmente"),
    "relationship_community": ("friend", "family", "community", "together", "朋友", "家人", "社区", "一起", "amigo", "familia", "comunidad"),
    "home_food": ("home", "cook", "food", "meal", "家里", "做饭", "食物", "casa", "cocinar", "comida"),
    "travel_place": ("travel", "trip", "visit", "place", "旅行", "出行", "地方", "viaje", "visitar", "lugar"),
    "maintenance_repair": ("repair", "fix", "maintain", "broken", "修复", "维修", "坏了", "reparar", "arreglar"),
    "creative_expression": ("write", "design", "create", "art", "写作", "设计", "创作", "escribir", "diseño", "crear"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_jsonl(path: Path = DATASET_PATH) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: {exc}") from exc
    return rows


def jsonl_text(rows: Iterable[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n"


def strip_markup(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"!?\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"```.*?```", " ", value, flags=re.DOTALL)
    value = re.sub(r"`[^`]*`", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def has_forbidden_content(value: str) -> str | None:
    for pattern in CONTACT_PATTERNS:
        if pattern.search(value):
            return "contact_or_locator"
    for pattern in HIGH_SENSITIVITY_PATTERNS:
        if pattern.search(value):
            return "high_sensitivity"
    for pattern in MINOR_PATTERNS:
        if pattern.search(value):
            return "minor_reference"
    return None


def lexical_units(value: str, language: str) -> list[str]:
    if language == "zh":
        return re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+", value)
    return re.findall(r"[\w'-]+", value, flags=re.UNICODE)


def minimal_excerpt(value: str, language: str) -> str | None:
    value = strip_markup(value)
    if has_forbidden_content(value):
        return None
    pieces = re.split(r"(?<=[.!?。！？；;])\s+|[\r\n]+", value)
    candidates = pieces or [value]
    for candidate in candidates:
        candidate = candidate.strip(" -–—:：/*#")
        units = lexical_units(candidate, language)
        if 5 <= len(units) <= 25 and 12 <= len(candidate) <= 220:
            return candidate
    units = lexical_units(value, language)
    if 5 <= len(units):
        if language == "zh":
            shortened = "".join(units[:25])
        else:
            shortened = " ".join(units[:25])
        if 12 <= len(shortened) <= 220 and not has_forbidden_content(shortened):
            return shortened
    return None


def classify_domain(value: str, fallback: str) -> str:
    lowered = value.lower()
    scores = {
        domain: sum(1 for term in terms if term in lowered)
        for domain, terms in DOMAIN_PATTERNS.items()
    }
    best, score = max(scores.items(), key=lambda item: (item[1], item[0]))
    return best if score else fallback


def assign_grouped_time_splits(rows: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["source_family"], row["source_group"])].append(row)
    by_family: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for (family, group), members in groups.items():
        newest = max(member["source_created_at"] for member in members)
        by_family[family].append((newest, group))
    split_for: dict[tuple[str, str], str] = {}
    for family, values in by_family.items():
        values.sort()
        count = len(values)
        train_end = max(1, int(count * 0.70))
        test_end = max(train_end + 1, int(count * 0.85)) if count > 2 else count
        for index, (_, group) in enumerate(values):
            split = "train" if index < train_end else "test" if index < test_end else "heldout"
            split_for[(family, group)] = split
    for row in rows:
        row["split"] = split_for[(row["source_family"], row["source_group"])]


def audit_records(rows: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[str] = []
    required = {
        "schema_version", "record_id", "source_id", "source_family", "source_group",
        "url", "source_created_at", "collected_at", "license", "attribution",
        "access_method", "robots_boundary", "language", "domain", "subject_id",
        "split", "deidentification", "deletion_pointer", "evidence_text", "layer",
    }
    ids: set[str] = set()
    sources: set[str] = set()
    group_splits: dict[tuple[str, str], set[str]] = defaultdict(set)
    subject_splits: dict[tuple[str, str], set[str]] = defaultdict(set)
    texts: dict[str, str] = {}
    for row in rows:
        missing = required - row.keys()
        if missing:
            errors.append(f"{row.get('record_id', '<unknown>')}: missing {sorted(missing)}")
            continue
        record_id = row["record_id"]
        if record_id in ids:
            errors.append(f"duplicate record_id: {record_id}")
        ids.add(record_id)
        if row["source_id"] in sources:
            errors.append(f"duplicate source_id: {row['source_id']}")
        sources.add(row["source_id"])
        if row["source_family"] not in ALLOWED_FAMILIES:
            errors.append(f"{record_id}: source family is not allowed")
        parsed_url = urllib.parse.urlparse(row["url"])
        allowed_hosts = ALLOWED_HOSTS.get(row["source_family"], ())
        if parsed_url.scheme != "https" or not any(
            parsed_url.hostname == host or parsed_url.hostname.endswith("." + host)
            for host in allowed_hosts
            if parsed_url.hostname
        ):
            errors.append(f"{record_id}: source URL is outside the HTTPS allowlist")
        if row["license"] not in ALLOWED_LICENSES:
            errors.append(f"{record_id}: license is not allowed")
        if row["language"] not in ALLOWED_LANGUAGES:
            errors.append(f"{record_id}: language is not allowed")
        if row["split"] not in {"train", "test", "heldout"}:
            errors.append(f"{record_id}: invalid split")
        if row["layer"] != "human_source_evidence" or row.get("generated", False):
            errors.append(f"{record_id}: human evidence layer is not isolated")
        group_splits[(row["source_family"], row["source_group"])].add(row["split"])
        subject_splits[(row["source_group"], row["subject_id"])].add(row["split"])
        for field in ("source_created_at", "collected_at"):
            try:
                datetime.fromisoformat(row[field].replace("Z", "+00:00"))
            except ValueError:
                errors.append(f"{record_id}: invalid {field}")
        units = lexical_units(row["evidence_text"], row["language"])
        if not 5 <= len(units) <= 25:
            errors.append(f"{record_id}: excerpt is not 5-25 lexical units")
        forbidden = has_forbidden_content(row["evidence_text"])
        if forbidden:
            errors.append(f"{record_id}: {forbidden}")
        normalized = " ".join(lexical_units(row["evidence_text"].lower(), row["language"]))
        if normalized in texts:
            errors.append(f"exact duplicate: {texts[normalized]} / {record_id}")
        texts[normalized] = record_id
        if row["deletion_pointer"] != {
            "source_id": row["source_id"], "source_group": row["source_group"]
        }:
            errors.append(f"{record_id}: invalid deletion pointer")

    group_leaks = {str(key): sorted(value) for key, value in group_splits.items() if len(value) > 1}
    subject_leaks = {str(key): sorted(value) for key, value in subject_splits.items() if len(value) > 1}
    if group_leaks:
        errors.append(f"source group split leakage: {len(group_leaks)}")
    if subject_leaks:
        errors.append(f"subject split leakage: {len(subject_leaks)}")
    near_duplicates = find_near_duplicates(rows)
    if near_duplicates:
        errors.append(f"near duplicate pairs: {len(near_duplicates)}")
    domains = Counter(row.get("domain") for row in rows)
    families = Counter(row.get("source_family") for row in rows)
    languages = Counter(row.get("language") for row in rows)
    splits = Counter(row.get("split") for row in rows)
    thresholds = {
        "records_min": 10_000,
        "source_groups_min": 1_000,
        "source_families_min": 4,
        "languages_min": 3,
        "required_languages": ["zh", "en"],
        "domains_min": 12,
    }
    threshold_failures = []
    group_count = len(group_splits)
    if len(rows) < thresholds["records_min"]:
        threshold_failures.append("records")
    if group_count < thresholds["source_groups_min"]:
        threshold_failures.append("source_groups")
    if len(families) < thresholds["source_families_min"]:
        threshold_failures.append("source_families")
    if len(languages) < thresholds["languages_min"] or not {"zh", "en"} <= set(languages):
        threshold_failures.append("languages")
    if len(domains) < thresholds["domains_min"]:
        threshold_failures.append("domains")
    return {
        "passed": not errors and not threshold_failures,
        "errors": errors,
        "threshold_failures": threshold_failures,
        "thresholds": thresholds,
        "record_count": len(rows),
        "source_group_count": group_count,
        "source_family_distribution": dict(sorted(families.items())),
        "language_distribution": dict(sorted(languages.items())),
        "domain_distribution": dict(sorted(domains.items())),
        "split_distribution": dict(sorted(splits.items())),
        "source_group_split_leaks": group_leaks,
        "subject_split_leaks": subject_leaks,
        "pii_high_sensitivity_minor_hits": sum(
            1 for error in errors if any(term in error for term in ("contact", "sensitivity", "minor"))
        ),
        "exact_duplicate_count": sum(1 for error in errors if error.startswith("exact duplicate")),
        "near_duplicate_count": len(near_duplicates),
        "near_duplicate_sample": near_duplicates[:20],
    }


def _token_set(row: dict[str, Any]) -> set[str]:
    return {unit.lower() for unit in lexical_units(row["evidence_text"], row["language"])}


def _minhash(tokens: set[str], seeds: int = 12) -> tuple[int, ...]:
    if not tokens:
        return tuple(0 for _ in range(seeds))
    return tuple(
        min(int.from_bytes(hashlib.blake2b(f"{seed}:{token}".encode(), digest_size=8).digest(), "big") for token in tokens)
        for seed in range(seeds)
    )


def find_near_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, int, tuple[int, int]], list[int]] = defaultdict(list)
    token_sets = [_token_set(row) for row in rows]
    candidates: set[tuple[int, int]] = set()
    for index, (row, tokens) in enumerate(zip(rows, token_sets, strict=True)):
        signature = _minhash(tokens)
        for band in range(6):
            key = (row["language"], band, signature[band * 2 : band * 2 + 2])
            for other in buckets[key]:
                candidates.add((other, index))
            buckets[key].append(index)
    output = []
    for left, right in sorted(candidates):
        union = token_sets[left] | token_sets[right]
        if not union:
            continue
        similarity = len(token_sets[left] & token_sets[right]) / len(union)
        if similarity >= 0.9:
            output.append(
                {
                    "left": rows[left]["record_id"],
                    "right": rows[right]["record_id"],
                    "jaccard": round(similarity, 4),
                }
            )
    return output


def remove_near_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairs = find_near_duplicates(rows)
    rejected = {pair["right"] for pair in pairs}
    return [row for row in rows if row["record_id"] not in rejected]


def source_host(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or ""
