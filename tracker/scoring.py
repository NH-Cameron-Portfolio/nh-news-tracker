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


# Material-news topic buckets — being in any of these is positive evidence the article
# is genuinely business news, not promotional / community / sponsorship fluff.
MATERIAL_TOPICS = {
    "strategy_and_transformation",
    "leadership_and_governance",
    "regulatory_and_compliance",
    "financial_distress_or_crisis",
    "ma_and_corporate_activity",
    "workforce_and_capability",
    "investment_and_capex",
    # v8b: pollution/sewage incidents and major service failures are material business news
    # for utilities — they trigger regulatory action and reputational consequences
    "customer_and_service",
    "technology_and_data",
}

# High-credibility source buckets (FT, Reuters, BBC, Bloomberg, etc.) and primary sources
# (Ofgem/Ofwat/RNS) count as material signal even when no topic keywords are present.
MATERIAL_SOURCE_BUCKETS = ("primary_source", "high_credibility")

# Promotional / non-material phrase patterns. If a title matches one of these AND has no
# other material signal, the article is treated as MENTIONED (or discarded depending on score)
# rather than RELEVANT/PRIORITY. These don't penalise score — they only block tier promotion.
NON_MATERIAL_PATTERNS = [
    r"\bpartner award", r"\bsmartphone", r"\bnow offering",
    r"\b(connects?|sponsor) (to|of) (UEFA|FIFA|Premier League|Euro \d+|Olympics?)",
    r"\bsponsorship deal", r"\b(launches?|introduces?) (the )?(new |latest )?(?!.*5G|.*FTTP|.*fibre)\w+\s+(phone|handset|tablet|device)",
    r"\bcommunity (event|fund|scheme|project)\b(?!.*(?:Ofgem|Ofwat|Ofcom|fine|enforcement))",
    r"\bcompetition (?!and markets|commission|authority|CMA)",
    r"\bawakens? your soul", r"\bawaken your soul",
    r"\bexpect delays\b", r"\bcautioned motorists",
    r"\bjoins .* as Head of (People|HR|Culture|Diversity)",
    r"\bjoins .* as .* (India|Singapore|Australia|Hong Kong|UAE)\b",
    r"\bpartners? with .* (charity|community|school)\b",
]


def _has_material_signal(item: NewsItem, credibility_cfg: dict) -> bool:
    """
    Does the article exhibit ANY signal that it's material business news (vs promotional fluff)?

    A material signal is any of:
      - Any MATERIAL_TOPICS keyword bucket hit
      - Article comes from a primary source (Ofgem/Ofwat/RNS)
      - Article comes from a high-credibility business outlet (FT/Reuters/BBC/Bloomberg/etc.)
      - Article comes from sector trade press
      - Title or summary mentions a substantial £ or $ figure (≥ £10m / $10m)
        — captures investment, M&A, financial impact stories
    """
    # Topic-based signal
    if any(t in MATERIAL_TOPICS for t in item.matched_topics):
        return True
    # Source-based signal
    for bucket in ("primary_source", "high_credibility", "trade_press"):
        if item.source_domain in credibility_cfg.get(bucket, {}).get("domains", []):
            return True
    # Google News title suffix can also tell us the source is high-credibility
    title_low = (item.title or "").lower()
    HIGH_CRED_SUFFIXES = [
        "- financial times", "- ft.com", "- the times", "- reuters", "- bbc",
        "- bbc news", "- bbc business", "- bloomberg", "- the guardian",
        "- telegraph", "- the telegraph", "- the economist", "- economist",
        "- wall street journal", "- wsj", "- bloomberg.com",
    ]
    if any(title_low.endswith(s) for s in HIGH_CRED_SUFFIXES):
        return True
    # Substantial financial figure (≥ £10m or $10m) — strong signal of material news
    combined = (item.title or "") + " " + (item.summary or "")[:400]
    if _has_substantial_money(combined):
        return True
    return False


# Match £ or $ figures of £10m+ ($10m+) or £1bn+. Matches: £42m, £230m, £4.3bn, $5.8 billion, £400 million, etc.
_MONEY_PATTERN = re.compile(
    r"(?:£|\$)\s*"                                      # currency
    r"(?:"
    r"\d{1,3}(?:[\.,]\d+)?\s*(?:bn|billion|tr|trillion)" # ≥1bn
    r"|"
    r"(?:[1-9]\d|\d{3,})(?:[\.,]\d+)?\s*(?:m|mn|million)"  # ≥10m
    r"|"
    r"(?:[1-9]\d|\d{3,})(?:[\.,]\d+)?\s*(?:bn|m|million)?" # large bare number with £/$
    r")",
    re.IGNORECASE,
)


def _has_substantial_money(text: str) -> bool:
    """True if the text contains a substantial currency figure (£10m+ or $10m+ or £1bn+)."""
    return bool(_MONEY_PATTERN.search(text))


def _matches_non_material_pattern(item: NewsItem) -> bool:
    """True if title or summary matches a known non-material pattern (sponsorship,
    smartphone launches, junior overseas hires, community events, etc)."""
    text = ((item.title or "") + " " + (item.summary or "")[:400]).lower()
    return any(re.search(pat, text, re.IGNORECASE) for pat in NON_MATERIAL_PATTERNS)


def assign_tier(item: NewsItem, credibility_cfg: dict | None = None) -> None:
    """
    Set item.tier based on score AND materiality.

    v8 changes:
      - Items must demonstrate a MATERIAL signal (topic, source-credibility, or trade press)
        to be eligible for PRIORITY or RELEVANT tier
      - Items matching NON_MATERIAL_PATTERNS (partner awards, smartphone launches,
        sponsorship deals, junior overseas hires, community events) are capped at MENTIONED
        regardless of score
      - Net effect: client-name-mention alone is no longer enough to make RELEVANT tier;
        the article must actually contain business news
    """
    # Base score-based tier
    if item.score >= 10:
        base_tier = "PRIORITY"
    elif item.score >= 5:
        base_tier = "RELEVANT"
    elif item.score >= 2:
        base_tier = "MENTIONED"
    else:
        base_tier = "DISCARDED"

    # Materiality gate: PRIORITY and RELEVANT both require a material signal
    if base_tier in ("PRIORITY", "RELEVANT") and credibility_cfg is not None:
        if not _has_material_signal(item, credibility_cfg):
            base_tier = "MENTIONED"
        if _matches_non_material_pattern(item):
            base_tier = "MENTIONED"

    item.tier = base_tier


def score_and_tier(
    items: list[NewsItem],
    topics: dict,
    credibility_cfg: dict,
    exclusions: dict,
) -> list[NewsItem]:
    """Score every item, assign tiers, log distribution."""
    for item in items:
        score_item(item, topics, credibility_cfg, exclusions)
        assign_tier(item, credibility_cfg)

    distribution = {"PRIORITY": 0, "RELEVANT": 0, "MENTIONED": 0, "DISCARDED": 0}
    for item in items:
        distribution[item.tier] += 1

    log.info("Score distribution: %s", distribution)
    return items
