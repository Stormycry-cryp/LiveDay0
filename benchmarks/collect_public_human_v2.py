from __future__ import annotations

import argparse
import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from public_human_recall_v2.common import (
    AUDIT_PATH,
    DATASET_PATH,
    SOURCES_PATH,
    assign_grouped_time_splits,
    audit_records,
    classify_domain,
    jsonl_text,
    lexical_units,
    minimal_excerpt,
    remove_near_duplicates,
    stable_hash,
    utc_now,
)


USER_AGENT = "LiveDay0-public-human-benchmark-v2/1.0 (research; minimal evidence; no profiles)"
TARGETS = {
    "wikimedia_zh": 3_000,
    "wikimedia_en": 3_000,
    "wikimedia_es": 10_500,
    "fedora_discussion": 2_050,
    "zcash_forum": 1_300,
}
DISCOURSE = {
    "fedora_discussion": {
        "base": "https://discussion.fedoraproject.org",
        "license": "CC BY-SA 4.0",
        "fallback_domain": "maintenance_repair",
    },
    "zcash_forum": {
        "base": "https://forum.zcashcommunity.com",
        "license": "CC BY-SA 4.0",
        "fallback_domain": "task_project",
    },
}
WIKIMEDIA_DUMPS = {
    "zh": [
        ("zhwikibooks", "https://zh.wikibooks.org"),
        ("zhwikinews", "https://zh.wikinews.org"),
    ],
    "en": [
        ("simplewiktionary", "https://simple.wiktionary.org"),
        ("simplewikibooks", "https://simple.wikibooks.org"),
        ("simplewikiquote", "https://simple.wikiquote.org"),
    ],
    "es": [
        ("eswikibooks", "https://es.wikibooks.org"),
        ("eswikinews", "https://es.wikinews.org"),
        ("eswikiquote", "https://es.wikiquote.org"),
        ("eswikivoyage", "https://es.wikivoyage.org"),
    ],
}


def fetch(url: str, *, retries: int = 4) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt + 1 < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == retries:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def fetch_json(url: str) -> dict[str, Any]:
    return json.loads(fetch(url))


def audit_sources_live() -> dict[str, Any]:
    registry = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    checks = []
    for source in registry:
        if source["family"] == "stackexchange_v1_frozen":
            checks.append(
                {
                    "family": source["family"],
                    "operator": source["operator"],
                    "license": source["license"],
                    "terms_url": source["terms_url"],
                    "terms_retrieved": False,
                    "terms_sha256": None,
                    "robots": [],
                    "collection_enabled": False,
                    "boundary": source["collection_boundary"],
                    "audit_note": "No v2 request was made. The frozen v1 source/license audit is retained, and current policy prevents scale collection.",
                }
            )
            continue
        terms_body = fetch(source["terms_url"])
        robots_checks = []
        for robots_url in source["robots_urls"]:
            body = fetch(robots_url)
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(body.decode("utf-8", errors="replace").splitlines())
            host = urllib.parse.urlparse(robots_url).netloc
            if source["family"] == "stackexchange_v1_frozen":
                access_url = f"https://{host}/"
            else:
                access_url = f"https://{host}/posts.json"
            robots_checks.append(
                {
                    "url": robots_url,
                    "sha256": stable_hash(body.decode("utf-8", errors="replace")),
                    "access_url": access_url,
                    "allowed_for_user_agent": parser.can_fetch(USER_AGENT, access_url),
                }
            )
        checks.append(
            {
                "family": source["family"],
                "operator": source["operator"],
                "license": source["license"],
                "terms_url": source["terms_url"],
                "terms_retrieved": True,
                "terms_sha256": stable_hash(terms_body.decode("utf-8", errors="replace")),
                "robots": robots_checks,
                "collection_enabled": source["family"] != "stackexchange_v1_frozen",
                "boundary": source["collection_boundary"],
            }
        )
    return {
        "schema_version": "public-human-source-audit-v2",
        "checked_at": utc_now(),
        "user_agent": USER_AGENT,
        "checks": checks,
        "passed": all(
            item["terms_retrieved"]
            and all(robot["allowed_for_user_agent"] for robot in item["robots"])
            for item in checks
            if item["collection_enabled"]
        ),
    }


def _speaker_labels(rows: list[dict[str, Any]]) -> None:
    by_group: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        by_group[row["source_group"]].add(row.pop("_speaker"))
    labels = {
        (group, speaker): f"thread-local-speaker-{index + 1:03d}"
        for group, speakers in by_group.items()
        for index, speaker in enumerate(sorted(speakers))
    }
    for row in rows:
        speaker = row.pop("_speaker_value")
        row["subject_id"] = labels[(row["source_group"], speaker)]


def collect_discourse(family: str, target: int, collected_at: str) -> list[dict[str, Any]]:
    config = DISCOURSE[family]
    base = config["base"]
    rows: list[dict[str, Any]] = []
    before: int | None = None
    seen_ids: set[int] = set()
    duplicate_texts: set[str] = set()
    pages = 0
    while len(rows) < target and pages < 240:
        query = "" if before is None else "?" + urllib.parse.urlencode({"before": before})
        payload = fetch_json(f"{base}/posts.json{query}")
        posts = payload.get("latest_posts", [])
        if not posts:
            break
        pages += 1
        before = min(int(post["id"]) for post in posts)
        for post in posts:
            post_id = int(post["id"])
            if post_id in seen_ids:
                continue
            seen_ids.add(post_id)
            if post.get("deleted_at") or post.get("hidden") or post.get("user_deleted"):
                continue
            if int(post.get("post_type", 1)) != 1:
                continue
            raw = post.get("raw") or post.get("cooked") or ""
            excerpt = minimal_excerpt(raw, "en")
            if not excerpt:
                continue
            normalized = " ".join(excerpt.lower().split())
            if normalized in duplicate_texts:
                continue
            duplicate_texts.add(normalized)
            topic_id = int(post["topic_id"])
            source_group = f"{family}:topic:{topic_id}"
            post_url = post.get("post_url") or f"/t/{post.get('topic_slug', 'topic')}/{topic_id}/{post.get('post_number', 1)}"
            url = urllib.parse.urljoin(base, post_url)
            speaker = str(post.get("username") or post.get("user_id") or f"post-{post_id}")
            rows.append(
                {
                    "schema_version": "public-human-evidence-v2",
                    "record_id": f"{family}-post-{post_id}",
                    "source_id": f"{family}:post:{post_id}",
                    "source_family": family,
                    "source_group": source_group,
                    "url": url,
                    "source_created_at": post["created_at"],
                    "collected_at": collected_at,
                    "license": config["license"],
                    "attribution": {"operator": family, "source_url": url, "modified": "short excerpt; markup, identity, and unsafe detail omitted"},
                    "access_method": "public Discourse /posts.json",
                    "robots_boundary": "Endpoint allowed by robots audit; no login, search, profile, private, or deleted-content access.",
                    "language": "en",
                    "domain": classify_domain(excerpt, config["fallback_domain"]),
                    "_speaker": speaker,
                    "_speaker_value": speaker,
                    "split": "pending",
                    "deidentification": "Username, display name, user ID, avatar, badges, mentions, reply identity, links, and profile data omitted; subject label is scoped to this topic only.",
                    "deletion_pointer": {"source_id": f"{family}:post:{post_id}", "source_group": source_group},
                    "evidence_text": excerpt,
                    "layer": "human_source_evidence",
                    "generated": False,
                }
            )
            if len(rows) >= target:
                break
        time.sleep(0.15)
    _speaker_labels(rows)
    return rows


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _tag(child) == name:
            return child.text
    return None


def clean_wikimedia_comment(value: str, language: str) -> str | None:
    value = value.strip()
    if re.search(
        r"^(?:revert(?:ed)?|undo|undid|rollback|rv\b|回退|撤销|撤銷|revertid[oa]|deshacer)",
        value,
        re.IGNORECASE,
    ):
        return None
    value = re.sub(r"^(?:wWPAES|WP\w{0,8})+", "", value, flags=re.IGNORECASE)
    prefixes = (
        r"^已建立頁面內容為[:：]?",
        r"^已建立页面内容为[:：]?",
        r"^创建页面，内容为[:：]?",
        r"^建立內容為[:：]?",
        r"^created page with(?: content)?[:：]?",
        r"^new page[:：]?",
        r"^página creada con[:：]?",
        r"^página nueva[:：]?",
    )
    for pattern in prefixes:
        value = re.sub(pattern, "", value, flags=re.IGNORECASE).strip(" «»\"'.,，。:：")
    if re.fullmatch(r"[A-Za-z0-9_./:{}|\- ]+", value or ""):
        return None
    return value or None


def collect_wikimedia(language: str, target: int, collected_at: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    duplicate_texts: set[str] = set()
    for database, base in WIKIMEDIA_DUMPS[language]:
        dump_url = f"https://dumps.wikimedia.org/{database}/latest/{database}-latest-stub-meta-history.xml.gz"
        request = urllib.request.Request(dump_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response, gzip.GzipFile(fileobj=response) as stream:
            for _, page in ET.iterparse(stream, events=("end",)):
                if _tag(page) != "page":
                    continue
                namespace = _child_text(page, "ns")
                page_id_text = _child_text(page, "id")
                if namespace != "1" or not page_id_text:
                    page.clear()
                    continue
                page_id = int(page_id_text)
                for revision in (child for child in page if _tag(child) == "revision"):
                    revision_id_text = _child_text(revision, "id")
                    timestamp = _child_text(revision, "timestamp")
                    comment = _child_text(revision, "comment") or ""
                    contributor = next((child for child in revision if _tag(child) == "contributor"), None)
                    if contributor is None or any(_tag(child) == "ip" for child in contributor):
                        continue
                    username = _child_text(contributor, "username")
                    if not revision_id_text or not timestamp or not username:
                        continue
                    revision_id = int(revision_id_text)
                    if revision_id in seen_ids:
                        continue
                    seen_ids.add(revision_id)
                    comment = re.sub(r"^/\*.*?\*/\s*", "", comment).strip()
                    comment = clean_wikimedia_comment(comment, language) or ""
                    excerpt = minimal_excerpt(comment, language)
                    if not excerpt:
                        continue
                    normalized = " ".join(excerpt.lower().split())
                    if normalized in duplicate_texts:
                        continue
                    duplicate_texts.add(normalized)
                    source_group = f"wikimedia:{database}:talk-page:{page_id}"
                    source_id = f"wikimedia:{database}:revision:{revision_id}"
                    revision_url = f"{base}/w/index.php?oldid={revision_id}"
                    rows.append(
                        {
                            "schema_version": "public-human-evidence-v2",
                            "record_id": f"wikimedia-{database}-revision-{revision_id}",
                            "source_id": source_id,
                            "source_family": "wikimedia",
                            "source_group": source_group,
                            "url": revision_url,
                            "source_created_at": timestamp,
                            "collected_at": collected_at,
                            "license": "CC BY-SA 4.0",
                            "attribution": {"operator": "Wikimedia Foundation", "source_url": revision_url, "modified": "talk-page revision summary only; identity and unsafe detail omitted"},
                            "access_method": f"official Wikimedia {database} stub-meta-history dump",
                            "robots_boundary": "Official downloadable dump; no live project-page crawling or API request.",
                            "language": language,
                            "domain": classify_domain(excerpt, "relationship_community"),
                            "_speaker": username,
                            "_speaker_value": username,
                            "split": "pending",
                            "deidentification": "Editor name, account/IP identity, page title, and profile are omitted; IP-authored revisions are excluded; subject label is scoped to this talk page only.",
                            "deletion_pointer": {"source_id": source_id, "source_group": source_group},
                            "evidence_text": excerpt,
                            "layer": "human_source_evidence",
                            "generated": False,
                        }
                    )
                    if len(rows) >= target:
                        break
                page.clear()
                if len(rows) >= target:
                    break
        if len(rows) >= target:
            break
    _speaker_labels(rows)
    return rows


def import_frozen_v1(collected_at: str) -> list[dict[str, Any]]:
    path = Path(__file__).with_name("public_human_recall") / "dataset.jsonl"
    original = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    rows = []
    for item in original:
        source_id = "stackexchange-v1:" + item["source_id"]
        source_group = "stackexchange-v1:" + item["source_group"]
        excerpt = item["evidence_text"]
        rows.append(
            {
                "schema_version": "public-human-evidence-v2",
                "record_id": "stackexchange-v1-" + item["record_id"],
                "source_id": source_id,
                "source_family": "stackexchange_v1_frozen",
                "source_group": source_group,
                "url": item["url"],
                "source_created_at": item["source_created_at"],
                "collected_at": collected_at,
                "license": item["license"],
                "attribution": {"operator": "Stack Exchange", "source_url": item["url"], "modified": "frozen v1 short excerpt; identity and unsafe detail omitted"},
                "access_method": "frozen v1 official-API record; no v2 network request",
                "robots_boundary": "Not recollected in v2 because current automated-access policy is incompatible with scale benchmarking.",
                "language": "en",
                "domain": classify_domain(excerpt, "task_project"),
                "subject_id": "thread-local-" + item["subject_id"],
                "split": "pending",
                "deidentification": item["deidentification"],
                "deletion_pointer": {"source_id": source_id, "source_group": source_group},
                "evidence_text": excerpt,
                "layer": "human_source_evidence",
                "generated": False,
            }
        )
    return rows


def cached_collection(key: str, collector, *, refresh: bool) -> list[dict[str, Any]]:
    cache_path = Path("/tmp") / f"liveday0-public-human-v2c-{key}.json"
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    rows = collector()
    try:
        cache_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        if exc.errno != 28:
            raise
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect the licensed public-human v2 dataset.")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--skip-live-source-audit", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="Ignore source caches in /tmp.")
    args = parser.parse_args()

    if args.audit_only:
        rows = [json.loads(line) for line in DATASET_PATH.read_text(encoding="utf-8").splitlines() if line]
        print(json.dumps(audit_records(rows), ensure_ascii=False, indent=2))
        return

    source_audit = None if args.skip_live_source_audit else audit_sources_live()
    if source_audit is not None and not source_audit["passed"]:
        raise SystemExit(json.dumps(source_audit, ensure_ascii=False, indent=2))

    collected_at = utc_now()
    rows: list[dict[str, Any]] = []
    rows.extend(import_frozen_v1(collected_at))
    rows.extend(cached_collection("wikimedia-zh", lambda: collect_wikimedia("zh", TARGETS["wikimedia_zh"], collected_at), refresh=args.refresh))
    rows.extend(cached_collection("wikimedia-en", lambda: collect_wikimedia("en", TARGETS["wikimedia_en"], collected_at), refresh=args.refresh))
    rows.extend(cached_collection("wikimedia-es-extended", lambda: collect_wikimedia("es", TARGETS["wikimedia_es"], collected_at), refresh=args.refresh))
    rows.extend(cached_collection("fedora-2050", lambda: collect_discourse("fedora_discussion", TARGETS["fedora_discussion"], collected_at), refresh=args.refresh))
    rows.extend(cached_collection("zcash-1300", lambda: collect_discourse("zcash_forum", TARGETS["zcash_forum"], collected_at), refresh=args.refresh))

    # Exact excerpt duplicates cannot cross sources because that would make source attribution ambiguous.
    unique: list[dict[str, Any]] = []
    seen_text: set[tuple[str, str]] = set()
    for row in sorted(rows, key=lambda item: (item["source_family"], item["source_created_at"], item["source_id"])):
        if row["source_family"] == "wikimedia":
            cleaned = clean_wikimedia_comment(row["evidence_text"], row["language"])
            excerpt = minimal_excerpt(cleaned or "", row["language"])
            if not excerpt:
                continue
            row["evidence_text"] = excerpt
        units = lexical_units(row["evidence_text"], row["language"])
        if not 5 <= len(units) <= 25:
            continue
        key = (row["language"], " ".join(unit.lower() for unit in units))
        if key in seen_text:
            continue
        seen_text.add(key)
        unique.append(row)
    rows = unique
    rows = remove_near_duplicates(rows)
    assign_grouped_time_splits(rows)
    audit = audit_records(rows)
    DATASET_PATH.write_text(jsonl_text(rows), encoding="utf-8")
    if source_audit is not None:
        source_audit["collection"] = {
            "target_distribution": TARGETS,
            "actual_family_distribution": dict(sorted(Counter(row["source_family"] for row in rows).items())),
            "dataset_sha256": stable_hash(DATASET_PATH.read_text(encoding="utf-8")),
            "dataset_audit": audit,
        }
        AUDIT_PATH.write_text(json.dumps(source_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    if not audit["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
