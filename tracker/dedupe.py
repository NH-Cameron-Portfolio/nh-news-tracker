"""
dedupe.py — Deduplicate news items by URL and by fuzzy title match.

Strategy:
  1. Canonicalise URLs (strip tracking params, fragments) and dedupe exact matches.
  2. Cluster remaining items by fuzzy title similarity (rapidfuzz token_set_ratio >= 90).
  3. From each cluster, keep one "best" representative.

"Best" is decided by source credibility (higher wins), then by published_at (earliest wins, on the
assumption that the first publisher is closest to the source).
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from rapidfuzz import fuzz

from tracker.sources import NewsItem

log = logging.getLogger(__name__)

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src",
    "CMP", "cmp", "sh", "share",
}


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


def _credibility_score(item: NewsItem, credibility_cfg: dict) -> int:
    """Roughly score a source for 'best of cluster' selection. Independent of relevance scoring."""
    domain = item.source_domain
    score = 0
    for bucket in ("primary_source", "high_credibility", "trade_press"):
        if domain in credibility_cfg.get(bucket, {}).get("domains", []):
            score += credibility_cfg[bucket]["weight"]
    return score


def deduplicate(items: list[NewsItem], credibility_cfg: dict, fuzz_threshold: int = 90) -> list[NewsItem]:
    """Remove URL-duplicates and cluster near-duplicate titles."""
    # Stage A: canonical URL dedup
    by_url: dict[str, NewsItem] = {}
    for item in items:
        key = canonical_url(item.url)
        if key not in by_url:
            by_url[key] = item
        else:
            # Same URL — keep the version from the more credible source (rare; only matters if duplicate URLs
            # come in via different feeds, e.g., Google News + direct outlet feed).
            existing = by_url[key]
            if _credibility_score(item, credibility_cfg) > _credibility_score(existing, credibility_cfg):
                by_url[key] = item
    url_unique = list(by_url.values())
    log.info("Dedup stage A (URL): %d -> %d", len(items), len(url_unique))

    # Stage B: fuzzy title clustering
    clusters: list[list[NewsItem]] = []
    for item in url_unique:
        placed = False
        for cluster in clusters:
            # Compare against the representative (first item in each cluster) only — O(n*k) instead of O(n^2)
            rep = cluster[0]
            if fuzz.token_set_ratio(item.title, rep.title) >= fuzz_threshold:
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
        # Sort by (credibility desc, date asc) — best source first, earliest if tied
        cluster.sort(
            key=lambda it: (
                -_credibility_score(it, credibility_cfg),
                it.published_at.timestamp() if it.published_at else float("inf"),
            )
        )
        survivors.append(cluster[0])

    log.info("Dedup stage B (fuzzy title): %d -> %d clusters", len(url_unique), len(survivors))
    return survivors
