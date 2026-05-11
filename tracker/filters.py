"""
filters.py — The two filter stages:
  Stage 2: Client name detection (with disambiguation)
  Stage 3: Quality gate (length, stock-only mentions, sponsored, language, age)

Reads clients.json and exclusions.json. Each NewsItem either survives with .matched_clients populated,
or is dropped. Drops are logged with reasons.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Iterable

from tracker.sources import NewsItem

log = logging.getLogger(__name__)


# ---------- Client detection ----------

# Acronyms (3-5 uppercase letters, all caps) must be case-sensitive to avoid lowercase false positives.
_ACRONYM_PATTERN = re.compile(r"^[A-Z]{2,6}[A-Z0-9]?\.?$")


def _is_acronym(alias: str) -> bool:
    return bool(_ACRONYM_PATTERN.match(alias.replace(".", "")))


def _build_alias_regex(alias: str) -> re.Pattern:
    """
    Build a word-boundary regex for an alias.

    - Acronyms (SSEN, NESO, SGN): case-sensitive, strict word boundaries.
    - Full names (Thames Water): case-insensitive (will still hit 'thames water' or 'Thames Water'),
      but word boundaries enforced.

    Note we escape the alias content but allow flexible internal whitespace (so "SES  Water" still matches).
    """
    parts = [re.escape(p) for p in alias.split()]
    pattern = r"\b" + r"\s+".join(parts) + r"\b"
    flags = 0 if _is_acronym(alias) else re.IGNORECASE
    return re.compile(pattern, flags)


def _compile_client_patterns(clients: dict) -> dict[str, dict]:
    """Pre-compile regex patterns for every alias. Called once at startup."""
    compiled: dict[str, dict] = {}
    for client_id, cfg in clients.items():
        compiled[client_id] = {
            "canonical": cfg["canonical"],
            "sector": cfg["sector"],
            "exact_patterns": [_build_alias_regex(a) for a in cfg.get("exact_aliases", [])],
            "contextual_patterns": [_build_alias_regex(a) for a in cfg.get("contextual_aliases", [])],
            "requires_context": [c.lower() for c in cfg.get("requires_context", [])],
            "negative_context": [c.lower() for c in cfg.get("negative_context", [])],
        }
    return compiled


def _text_for_matching(item: NewsItem) -> tuple[str, str, str]:
    """
    Return (title, summary, combined) — preserves case for acronym matching.

    Google News appends " - Source Name" to article titles. When the source name
    happens to equal the client name (e.g. "...- Virgin Media O2"), the bare title
    would falsely match even if the article content has nothing to do with the client.
    We strip that trailing suffix before matching.
    """
    raw_title = item.title or ""
    # Strip the " - Source Name" / " | Source Name" / " – Source Name" suffix
    title = _TITLE_SUFFIX_RE.sub("", raw_title).strip()
    summary = item.summary or ""
    combined = f"{title}\n{summary}"
    return title, summary, combined


def _is_high_credibility(item: NewsItem) -> bool:
    """
    True if the article is from a top-tier business source where any mention of a tracked
    client is almost always material. We use this to bypass strict context-gating —
    the editorial filter at these outlets does the relevance work for us.
    """
    # Check 1: source domain (works for direct feeds, e.g. ft.com)
    HIGH_CRED_DOMAINS = {
        "ft.com", "reuters.com", "bbc.co.uk", "bbc.com",
        "bloomberg.com", "thetimes.com", "thetimes.co.uk",
        "theguardian.com", "telegraph.co.uk", "economist.com",
        "wsj.com",
    }
    if item.source_domain in HIGH_CRED_DOMAINS:
        return True
    # Check 2: Google News title suffix (works for items wrapped via news.google.com)
    suffix = (item.title or "").lower()
    HIGH_CRED_SUFFIXES = [
        "- financial times", "- ft.com", "- the times",
        "- reuters", "- bbc", "- bbc news", "- bbc business",
        "- bloomberg", "- the guardian", "- telegraph",
        "- the telegraph", "- the economist", "- economist",
        "- wall street journal", "- wsj",
    ]
    return any(suffix.endswith(s) for s in HIGH_CRED_SUFFIXES)


def detect_clients(items: list[NewsItem], clients: dict) -> list[NewsItem]:
    """
    For each item, populate .matched_clients with canonical names of any client(s) detected.
    Items with no matches are dropped.

    v6 logic:
      - For most sources: strict matching (exact alias OR contextual alias + required context).
      - For high-credibility sources (FT, Reuters, BBC, Bloomberg, Times, Telegraph, Guardian, Economist):
        the context-gating requirement is dropped. Any mention of a client alias counts.
        Rationale: these outlets don't write filler about regulated utilities; if they're writing
        about a client by name, it's almost always material news. Saves us from missing major
        stories like "BT is back" where the headline doesn't include the keywords we'd otherwise require.
      - Negative context still applies (e.g. "Vodafone Idea India" still won't match Vodafone UK).
    """
    patterns = _compile_client_patterns(clients)
    survivors: list[NewsItem] = []

    for item in items:
        title, summary, combined = _text_for_matching(item)
        combined_lower = combined.lower()
        high_cred = _is_high_credibility(item)
        matched: list[str] = []

        for client_id, p in patterns.items():
            # Negative context check applies in both regimes
            negative_hits = any(neg in combined_lower for neg in p["negative_context"])

            # Try exact aliases first (always strict)
            exact_hit = any(pat.search(combined) for pat in p["exact_patterns"])

            if exact_hit and not negative_hits:
                matched.append(p["canonical"])
                continue
            if exact_hit and negative_hits:
                # If the exact alias is also a multi-word distinctive phrase, allow it through
                # despite negative context.
                if any(len(a.split()) > 2 for a in clients[client_id].get("exact_aliases", [])):
                    if any(
                        _build_alias_regex(a).search(combined)
                        for a in clients[client_id].get("exact_aliases", [])
                        if len(a.split()) > 2
                    ):
                        matched.append(p["canonical"])
                        continue
                continue

            # Contextual aliases
            ctx_hit = any(pat.search(combined) for pat in p["contextual_patterns"])
            if ctx_hit and not negative_hits:
                if high_cred:
                    # High-credibility source: skip context-word requirement
                    matched.append(p["canonical"])
                elif any(ctx in combined_lower for ctx in p["requires_context"]):
                    matched.append(p["canonical"])

        if matched:
            # Deduplicate while preserving order
            seen = set()
            item.matched_clients = [c for c in matched if not (c in seen or seen.add(c))]
            survivors.append(item)

    log.info("Stage 2 (client detection): %d -> %d", len(items), len(survivors))
    return survivors


# ---------- Quality gate ----------

_STOCK_PATTERN_DEFAULT = re.compile(
    r"^[A-Z][A-Za-z &]+\s+(shares|stock)\s+(up|down|rise|fall|jump|slip|gain|drop|rally)\s*\d+%?$"
)

# Match trailing " - Source Name" suffix that Google News appends to article titles.
# Used to recover the real publisher when the URL is wrapped via news.google.com.
_TITLE_SUFFIX_RE = re.compile(r"\s+[-|–]\s+([^-|–]{2,60})$")


def _extract_real_source(title: str) -> str | None:
    """Pull the real source name out of a Google News-style title suffix."""
    if not title:
        return None
    m = _TITLE_SUFFIX_RE.search(title)
    if m:
        return m.group(1).strip()
    return None


def quality_gate(items: list[NewsItem], exclusions: dict) -> list[NewsItem]:
    """
    Apply hard rejects: length floor, language, age, sponsored markers, stock-only titles,
    blocked domains, drop-phrase titles, stock-noise domains.
    """
    min_chars = exclusions.get("min_summary_chars", 100)
    max_age_days = exclusions.get("max_age_days", 7)
    url_patterns = exclusions.get("url_patterns", [])
    drop_phrases = [p.lower() for p in exclusions.get("title_phrases_drop", [])]
    blocked_domains = set(exclusions.get("domains_blocked", []))
    stock_noise_domains = set(exclusions.get("stock_noise_domains", []))
    # Friendly-name version of the stock-noise sources, for matching against the
    # " - Source Name" suffix Google News appends to wrapped titles.
    stock_noise_names = set(n.lower() for n in exclusions.get("stock_noise_source_names", []))

    # Multi-pattern stock detection (each applied via .search() not .match())
    stock_patterns = [re.compile(p, re.IGNORECASE) for p in exclusions.get("stock_only_patterns", [])]
    # Back-compat: keep the old single regex as a fallback
    if not stock_patterns:
        stock_patterns = [re.compile(exclusions.get("title_regex_stock_only", _STOCK_PATTERN_DEFAULT.pattern), re.IGNORECASE)]

    # Entertainment / sponsorship noise patterns (drag shows, gig tickets, Priority by O2, etc)
    entertainment_patterns = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in exclusions.get("entertainment_noise_patterns", [])]

    # Topics that override stock-noise filter (these are genuinely material even if stock-y)
    OVERRIDE_TOPICS = {"financial_distress_or_crisis", "ma_and_corporate_activity", "regulatory_and_compliance", "leadership_and_governance"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    survivors: list[NewsItem] = []
    dropped = {"length": 0, "lang": 0, "age": 0, "sponsored": 0, "stock_only": 0, "domain": 0, "drop_phrase": 0, "stock_domain": 0, "stock_source": 0, "entertainment": 0}

    for item in items:
        # Domain blocklist
        if item.source_domain in blocked_domains:
            dropped["domain"] += 1
            continue
        # Sponsored / advertorial URL markers
        if any(pat in item.url.lower() for pat in url_patterns):
            dropped["sponsored"] += 1
            continue
        # Drop-phrase titles — check BOTH title and first 500 chars of summary
        # (catches entertainment phrases that appear in body but not title)
        title_lower = item.title.lower()
        summary_lower = (item.summary or "")[:500].lower()
        combined_lower = title_lower + " " + summary_lower
        if any(phrase.lower() in combined_lower for phrase in drop_phrases):
            dropped["drop_phrase"] += 1
            continue
        # Entertainment / sponsorship noise — regex over title + summary
        combined_text = (item.title or "") + " " + (item.summary or "")[:800]
        if any(p.search(combined_text) for p in entertainment_patterns):
            dropped["entertainment"] += 1
            continue
        # Extract the real source from Google News title suffix " - Source Name"
        # so we can check it against stock-noise sources even when URL is wrapped.
        real_source_name = _extract_real_source(item.title)

        # Stock-noise check (drop unless the article looks materially newsworthy)
        override_terms = [
            # Corporate events
            "acquisition", "merger", "takeover", "profit warning", "special administration",
            "downgrade", "upgrade", "fine", "enforcement", "investigation", "results",
            "equity raise", "rights issue", "rescue", "dividend cut", "dividend suspended",
            # Broker actions that ARE material (sector signal)
            "cuts target", "cuts price target", "raises target", "raises price target",
            "broker ratings", "citi cuts", "jpmorgan cuts", "barclays cuts",
            "downgrades", "upgrades",
            # Leadership — require an action verb, not bare role title (so 'CFO buys shares' doesn't qualify)
            "resigns", "appoints", "appointed", "steps down", "promoted to",
            "succeeded by", "named CEO", "named chair", "new chief executive",
            "new CEO", "new CFO", "new chair", "new chairman",
        ]
        title_has_override = any(t.lower() in title_lower for t in override_terms)

        # Apply stock-noise check via TWO signals: (a) url domain, (b) real source from Google News title suffix.
        is_stock_source = (
            item.source_domain in stock_noise_domains
            or (real_source_name and real_source_name.lower() in stock_noise_names)
        )
        if is_stock_source and not title_has_override:
            dropped["stock_domain"] += 1
            continue
        # Stock-only titles (regex search). Skip if from a primary source OR overridden by content.
        if "primary" not in item.feed_tags and not title_has_override:
            if any(p.search(item.title) for p in stock_patterns):
                dropped["stock_only"] += 1
                continue
        # Length floor
        if len(item.summary or "") < min_chars and not item.title:
            dropped["length"] += 1
            continue
        # Age check
        if item.published_at and item.published_at < cutoff:
            dropped["age"] += 1
            continue
        # Language check
        if _looks_non_english(item.title + " " + item.summary):
            dropped["lang"] += 1
            continue

        survivors.append(item)

    log.info("Stage 3 (quality gate): %d -> %d; dropped: %s", len(items), len(survivors), dropped)
    return survivors


def _looks_non_english(text: str) -> bool:
    """
    Cheap heuristic without forcing langdetect for short strings (it's unreliable).
    Looks for non-Latin scripts.
    """
    if not text:
        return False
    # If more than 30% of chars are outside basic Latin + common European accents, flag as non-English.
    non_latin = sum(1 for c in text if ord(c) > 0x024F and not c.isspace() and not c.isdigit())
    return non_latin > 0.3 * len(text)
