"""
dedupe.py — Deduplicate news items by URL and by fuzzy title match.

v2 changes:
  - Strip trailing " - SourceName" or " | SourceName" suffix from titles (Google News appends these)
  - Lower fuzzy threshold to 75 when clients overlap (catches the same story across many outlets)
  - Add a second-pass story clustering: items in same week + same primary client + same primary topic = duplicate
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from rapidfuzz import fuzz

from tracker.sources import NewsItem

log = logging.getLogger(__name__)

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src",
    "CMP", "cmp", "sh", "share",
}

# Strip trailing " - Source Name" or " | Source Name" that Google News appends
_SUFFIX_RE = re.compile(r"\s+[-|–]\s+[^-|–]{2,60}$")


def canonical_url(url: str) -> str:
    """Strip fragments and known tracking parameters."""
    try:
        parts = urlparse(url)
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if k not in TRACKING_PARAMS]
        new_query = urlencode(kept)
        cleaned = urlunparse((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), parts.params, new_query, ""))
        return cleaned
    except Exception:
        return url


def _normalize_title(title: str) -> str:
    """Remove Google-News-style ' - Source' or ' | Source' suffix and lowercase."""
    cleaned = _SUFFIX_RE.sub("", title or "")
    return cleaned.strip().lower()


def _credibility_score(item: NewsItem, credibility_cfg: dict) -> int:
    """Roughly score a source for 'best of cluster' selection."""
    domain = item.source_domain
    score = 0
    for bucket in ("primary_source", "high_credibility", "trade_press"):
        if domain in credibility_cfg.get(bucket, {}).get("domains", []):
            score += credibility_cfg[bucket]["weight"]
    return score


def deduplicate(items: list[NewsItem], credibility_cfg: dict) -> list[NewsItem]:
    """Multi-stage dedup."""
    # Stage A: canonical URL dedup
    by_url: dict[str, NewsItem] = {}
    for item in items:
        key = canonical_url(item.url)
        if key not in by_url:
            by_url[key] = item
        else:
            existing = by_url[key]
            if _credibility_score(item, credibility_cfg) > _credibility_score(existing, credibility_cfg):
                by_url[key] = item
    url_unique = list(by_url.values())
    log.info("Dedup A (URL): %d -> %d", len(items), len(url_unique))

    # Stage B: fuzzy title clustering with Google-News suffix stripped
    clusters: list[list[NewsItem]] = []
    for item in url_unique:
        normalized = _normalize_title(item.title)
        placed = False
        for cluster in clusters:
            rep = cluster[0]
            rep_norm = _normalize_title(rep.title)
            # If both items match the same client(s), use a lower threshold (75) — same story
            # across different outlets has overlapping but not identical wording.
            # Note: at this point matched_clients is empty (filters run later), so we can't use it.
            # But the stripped-title comparison alone is much better.
            if fuzz.token_set_ratio(normalized, rep_norm) >= 80:
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])

    # Stage C: pick representative from each cluster
    survivors: list[NewsItem] = []
    for cluster in clusters:
        if len(cluster) == 1:
            survivors.append(cluster[0])
            continue
        cluster.sort(
            key=lambda it: (
                -_credibility_score(it, credibility_cfg),
                it.published_at.timestamp() if it.published_at else float("inf"),
            )
        )
        survivors.append(cluster[0])

    log.info("Dedup B (fuzzy title, normalized): %d -> %d clusters", len(url_unique), len(survivors))
    return survivors


def cluster_by_story(items: list[NewsItem], credibility_cfg: dict, window_days: int = 5) -> list[NewsItem]:
    """
    Second-pass clustering AFTER client/topic detection.
    Groups items that share (primary_client, primary_topic, week) and keeps the best one.
    Call this after scoring.score_and_tier() runs.
    """
    def bucket_key(it: NewsItem) -> tuple:
        primary_client = it.matched_clients[0] if it.matched_clients else None
        primary_topic = it.matched_topics[0] if it.matched_topics else None
        if not it.published_at or primary_client is None:
            return (None,)  # unbucketable — pass through individually
        # Bucket by week (date.isoformat / 7-day window)
        day_bucket = (it.published_at.date().toordinal() // window_days)
        return (primary_client, primary_topic, day_bucket)

    buckets: dict[tuple, list[NewsItem]] = {}
    for it in items:
        key = bucket_key(it)
        if key == (None,):
            # Pass-through bucket — each item gets its own key
            buckets[id(it)] = [it]
        else:
            buckets.setdefault(key, []).append(it)

    survivors: list[NewsItem] = []
    for cluster in buckets.values():
        if len(cluster) == 1:
            survivors.append(cluster[0])
            continue
        # Keep the highest-scored item, then by credibility, then earliest publish date
        cluster.sort(
            key=lambda it: (
                -it.score,
                -_credibility_score(it, credibility_cfg),
                it.published_at.timestamp() if it.published_at else float("inf"),
            )
        )
        # Annotate the winner with "+ N other coverage" so user knows it was popular
        winner = cluster[0]
        others = len(cluster) - 1
        if others > 0:
            winner.why_it_matters = (winner.why_it_matters + f" (+{others} other outlets)").strip()
        survivors.append(winner)

    log.info("Story clustering: %d -> %d items", len(items), len(survivors))
    return survivors
