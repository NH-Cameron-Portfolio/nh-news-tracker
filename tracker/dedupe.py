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


# Maps lowercase title-suffix source names to a credibility weight.
# Used because Google News items have source_domain = news.google.com, hiding the real outlet.
_SUFFIX_SOURCE_WEIGHTS = {
    # High-credibility national/business press
    "financial times": 3, "ft.com": 3, "ft": 3,
    "reuters": 3, "bloomberg": 3, "bloomberg.com": 3,
    "the times": 3, "the sunday times": 3, "times": 3,
    "bbc": 3, "bbc news": 3, "bbc business": 3,
    "the guardian": 2, "guardian": 2,
    "the telegraph": 2, "telegraph": 2,
    "the economist": 3, "economist": 3,
    "wall street journal": 3, "wsj": 3,
    "city am": 2, "cityam": 2, "city a.m.": 2,
    "sky news": 2,
    # Trade press
    "utility week": 3, "current news": 2, "current±": 2,
    "ends report": 3, "edie": 2, "new civil engineer": 2,
    "net zero investor": 2, "the energyst": 2,
    "ispreview uk": 1, "ispreview": 1, "telecompaper": 1,
}


def _suffix_source_weight(title: str) -> int:
    """Recover a credibility weight from the ' - Source Name' suffix on a Google News title."""
    m = re.search(r"\s+[-|–]\s+([^-|–]{2,60})$", title or "")
    if not m:
        return 0
    src = m.group(1).strip().lower()
    return _SUFFIX_SOURCE_WEIGHTS.get(src, 0)


def _credibility_score(item: NewsItem, credibility_cfg: dict) -> int:
    """
    Roughly score a source for 'best of cluster' selection.

    v9c: also reads the Google News title suffix, because Google-wrapped items have
    source_domain = news.google.com which hides the real outlet. Without this, an FT
    article and a tabloid article about the same story were indistinguishable to the
    dedup, so the FT version could lose. Now the FT/Times version reliably wins.
    """
    domain = item.source_domain
    score = 0
    for bucket in ("primary_source", "high_credibility", "trade_press"):
        if domain in credibility_cfg.get(bucket, {}).get("domains", []):
            score += credibility_cfg[bucket]["weight"]
    # Add suffix-derived weight (for Google-News-wrapped items)
    score += _suffix_source_weight(item.title)
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


# Concept groups: titles sharing a concept group keyword are likely the same story
# even if their wording differs significantly.
_CONCEPT_GROUPS = [
    # Leadership departures (catches all the 'boss quits / CEO resigns / chief executive resigns' variants)
    {"quits", "resigns", "resign", "step down", "steps down", "stepping down",
     "departs", "departure", "leaves", "leaving", "exit", "exits",
     "ousted", "fired", "removed"},
    # Leadership appointments
    {"appoints", "appointed", "named ceo", "named chair", "names new",
     "joins", "joining", "new chief", "successor", "incoming"},
    # M&A / corporate activity
    {"acquires", "acquisition", "merger", "merges", "takeover", "buyout",
     "buys", "buy out", "takes full control", "exits joint venture",
     "stake sale", "divestment", "demerger"},
    # Financial / regulatory action
    {"fine", "fined", "penalty", "enforcement", "investigation", "probe",
     "downgrade", "downgraded", "credit rating", "going concern"},
    # Sewage / pollution incidents
    {"sewage spill", "sewage discharge", "pollution incident", "pollution incidents",
     "raw sewage", "untreated sewage"},
]


def _concept_match(title_a: str, title_b: str) -> bool:
    """True if both titles hit the same concept group — even if wording differs."""
    a_lower = title_a.lower()
    b_lower = title_b.lower()
    for group in _CONCEPT_GROUPS:
        a_hit = any(kw in a_lower for kw in group)
        b_hit = any(kw in b_lower for kw in group)
        if a_hit and b_hit:
            return True
    return False


def cluster_by_story(items: list[NewsItem], credibility_cfg: dict, window_days: int = 7, fuzz_threshold: int = 65) -> list[NewsItem]:
    """
    Second-pass clustering AFTER client/topic detection.

    Groups items that are likely the same story:
      1. Bucket items by (primary_client, week)
      2. Within each bucket, run fuzzy title clustering on suffix-stripped titles
         OR concept-based matching (e.g. "boss quits" + "CEO resigns" = same story)
      3. Keep the best representative from each cluster
      4. Annotate the winner with "(+N other outlets covered this)"

    v8 changes from v7:
      - Added concept-based matching as a fallback when fuzzy title score falls below threshold.
        This catches the "boss quits" vs "chief executive resigns" vs "what next after boss quits"
        case where titles vary too much for token_set_ratio but are clearly the same story.
    """
    def bucket_key(it: NewsItem) -> tuple:
        primary_client = it.matched_clients[0] if it.matched_clients else None
        if not it.published_at or primary_client is None:
            return None
        day_bucket = (it.published_at.date().toordinal() // window_days)
        return (primary_client, day_bucket)

    raw_buckets: dict[tuple, list[NewsItem]] = {}
    unbucketable: list[NewsItem] = []
    for it in items:
        key = bucket_key(it)
        if key is None:
            unbucketable.append(it)
        else:
            raw_buckets.setdefault(key, []).append(it)

    survivors: list[NewsItem] = list(unbucketable)

    for cluster in raw_buckets.values():
        if len(cluster) <= 1:
            survivors.extend(cluster)
            continue

        # Sub-cluster by fuzzy title similarity OR concept match
        sub_clusters: list[list[NewsItem]] = []
        for it in cluster:
            normalized = _normalize_title(it.title)
            placed = False
            for sub in sub_clusters:
                rep_norm = _normalize_title(sub[0].title)
                fuzzy_score = fuzz.token_set_ratio(normalized, rep_norm)
                concept_match = _concept_match(normalized, rep_norm)
                if fuzzy_score >= fuzz_threshold or concept_match:
                    sub.append(it)
                    placed = True
                    break
            if not placed:
                sub_clusters.append([it])

        for sub in sub_clusters:
            if len(sub) == 1:
                survivors.append(sub[0])
                continue
            # v9b: prefer higher tier first (PRIORITY > RELEVANT > MENTIONED > DISCARDED)
            # so a cluster doesn't accidentally promote a MENTIONED winner over a PRIORITY peer
            tier_rank = {"PRIORITY": 0, "RELEVANT": 1, "MENTIONED": 2, "DISCARDED": 3, "": 4}
            sub.sort(
                key=lambda it: (
                    tier_rank.get(it.tier, 4),                            # higher tier first
                    -it.score,                                            # then higher score
                    -_credibility_score(it, credibility_cfg),
                    it.published_at.timestamp() if it.published_at else float("inf"),
                )
            )
            winner = sub[0]
            others = len(sub) - 1
            if others > 0:
                annotation = f"+{others} other outlet{'s' if others > 1 else ''} covered this"
                if winner.why_it_matters:
                    winner.why_it_matters = f"{winner.why_it_matters} ({annotation})"
                else:
                    winner.why_it_matters = annotation
            survivors.append(winner)

    log.info("Story clustering: %d items -> %d after (client, week, fuzzy-or-concept) clustering", len(items), len(survivors))
    return survivors
