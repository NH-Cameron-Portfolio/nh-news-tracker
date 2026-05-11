"""
scoring.py — Stage 4 + Stage 5: numeric relevance scoring and tier assignment.

Scoring factors (see spec §5):
  +5 client name in title
  +3 client name in first 300 chars of body/summary
  +2 client mentioned 3+ times total
  +2 multiple tracked clients mentioned
  +4 RNS / regulator (primary source)
  +2 high-credibility source
  +2 trade press
  +2 per topic keyword bucket matched (cap +6)
  +1 article length > 500 words
 -10 excluded topic keyword present (handled in filters; redundant safety here)
  -5 stock-movement only (handled in filters; redundant safety)
  -2 PR wire republished

Tiers:
  PRIORITY: >= 10
  RELEVANT: 5–9
  MENTIONED: 2–4
  (Below 2: discarded — caller can choose to keep them in CSV but exclude from email)
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from tracker.sources import NewsItem

log = logging.getLogger(__name__)


def _domain_in_bucket(domain: str, credibility_cfg: dict, bucket: str) -> bool:
    return domain in credibility_cfg.get(bucket, {}).get("domains", [])


def _client_mentions_count(text: str, canonical: str) -> int:
    """Count occurrences of canonical client name (case-insensitive, word-bounded)."""
    pattern = re.compile(r"\b" + re.escape(canonical) + r"\b", re.IGNORECASE)
    return len(pattern.findall(text))


def _topic_hits(text_lower: str, topics: dict) -> list[str]:
    """Return list of topic-bucket names that have at least one keyword present."""
    hit_buckets: list[str] = []
    for bucket, keywords in topics.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                hit_buckets.append(bucket)
                break
    return hit_buckets


_SUFFIX_RE = __import__("re").compile(r"\s+[-|–]\s+[^-|–]{2,60}$")


def _summary_is_substantive(item: NewsItem) -> bool:
    """
    Returns True if the summary contains meaningful content beyond the title.

    Google News RSS often produces summaries that are just the title repeated or the title +
    source name. We strip the trailing " - Source" suffix from the title before comparing,
    so that the substance check only considers genuine title tokens.
    """
    title = _SUFFIX_RE.sub("", item.title or "").lower()
    summary = (item.summary or "").lower()
    if not summary:
        return False
    # If summary is shorter than the title, it's almost certainly a fragment, not content
    if len(summary) < len(title):
        return False
    title_tokens = set(title.split())
    summary_tokens = summary.split()
    if not summary_tokens:
        return False
    novel = [t for t in summary_tokens if t not in title_tokens]
    novelty_ratio = len(novel) / len(summary_tokens)
    novel_chars = len(summary) - len(title)
    # v4: relaxed thresholds — many genuine articles repeat the title heavily but add a sentence of body
    return novelty_ratio > 0.20 and novel_chars > 15


def score_item(
    item: NewsItem,
    topics: dict,
    credibility_cfg: dict,
    exclusions: dict,
) -> None:
    """Mutate the item: populate .score and .matched_topics."""
    title = item.title or ""
    summary = item.summary or ""
    combined = f"{title}\n{summary}"
    combined_lower = combined.lower()

    score = 0

    # ---- Client position scoring ----
    title_lower = title.lower()
    for canonical in item.matched_clients:
        if canonical.lower() in title_lower:
            score += 5
            break  # only credit title once

    # Only credit body match if the summary is substantive (not just a title repeat)
    if _summary_is_substantive(item):
        head = summary[:300].lower()
        for canonical in item.matched_clients:
            if canonical.lower() in head:
                score += 3
                break

    # Multi-mention bonus
    total_mentions = sum(_client_mentions_count(combined, c) for c in item.matched_clients)
    if total_mentions >= 3:
        score += 2

    # Multiple tracked clients
    if len(item.matched_clients) >= 2:
        score += 2

    # ---- Source credibility scoring ----
    domain = item.source_domain
    if _domain_in_bucket(domain, credibility_cfg, "primary_source"):
        score += 4
    elif _domain_in_bucket(domain, credibility_cfg, "high_credibility"):
        score += 2
    elif _domain_in_bucket(domain, credibility_cfg, "trade_press"):
        score += 2

    # ---- Topic scoring ----
    hit_buckets = _topic_hits(combined_lower, topics)
    item.matched_topics = hit_buckets
    score += min(len(hit_buckets) * 2, 6)

    # ---- Length bonus ----
    if len(summary.split()) > 500:
        score += 1

    # ---- Penalties ----
    pr_wires = exclusions.get("pr_wire_domains", [])
    if domain in pr_wires:
        score -= 2

    penalty_phrases = [p.lower() for p in exclusions.get("title_phrases_score_penalty", [])]
    if any(p in title_lower for p in penalty_phrases):
        score -= 5

    item.score = score


def assign_tier(item: NewsItem) -> None:
    """Set item.tier based on score."""
    if item.score >= 10:
        item.tier = "PRIORITY"
    elif item.score >= 5:
        item.tier = "RELEVANT"
    elif item.score >= 2:
        item.tier = "MENTIONED"
    else:
        item.tier = "DISCARDED"


def score_and_tier(
    items: list[NewsItem],
    topics: dict,
    credibility_cfg: dict,
    exclusions: dict,
) -> list[NewsItem]:
    """Score every item, assign tiers, log distribution."""
    for item in items:
        score_item(item, topics, credibility_cfg, exclusions)
        assign_tier(item)

    distribution = {"PRIORITY": 0, "RELEVANT": 0, "MENTIONED": 0, "DISCARDED": 0}
    for item in items:
        distribution[item.tier] += 1

    log.info("Score distribution: %s", distribution)
    return items
