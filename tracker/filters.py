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
    """Return (title, summary, combined) — preserves case for acronym matching."""
    title = item.title or ""
    summary = item.summary or ""
    combined = f"{title}\n{summary}"
    return title, summary, combined


def detect_clients(items: list[NewsItem], clients: dict) -> list[NewsItem]:
    """
    For each item, populate .matched_clients with canonical names of any client(s) detected.
    Items with no matches are dropped.
    """
    patterns = _compile_client_patterns(clients)
    survivors: list[NewsItem] = []

    for item in items:
        title, summary, combined = _text_for_matching(item)
        combined_lower = combined.lower()
        matched: list[str] = []

        for client_id, p in patterns.items():
            # Skip if a negative-context phrase is present and no STRONG exact-alias match exists
            negative_hits = any(neg in combined_lower for neg in p["negative_context"])

            # Try exact aliases first
            exact_hit = any(pat.search(combined) for pat in p["exact_patterns"])

            if exact_hit and not negative_hits:
                matched.append(p["canonical"])
                continue
            if exact_hit and negative_hits:
                # If the exact alias is also a multi-word distinctive phrase, allow it through
                # despite negative context. (e.g. an article mentioning both "SSE Thermal" and
                # "Scottish and Southern Electricity Networks" — the latter is unambiguous.)
                # Heuristic: full-name exact aliases > 2 words override negative_context.
                if any(len(a.split()) > 2 for a in clients[client_id].get("exact_aliases", [])):
                    if any(
                        _build_alias_regex(a).search(combined)
                        for a in clients[client_id].get("exact_aliases", [])
                        if len(a.split()) > 2
                    ):
                        matched.append(p["canonical"])
                        continue
                # Otherwise drop this match
                continue

            # Contextual aliases require both alias match AND context keyword AND no negative context
            ctx_hit = any(pat.search(combined) for pat in p["contextual_patterns"])
            if ctx_hit and not negative_hits:
                if any(ctx in combined_lower for ctx in p["requires_context"]):
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

    # Multi-pattern stock detection (each applied via .search() not .match())
    stock_patterns = [re.compile(p, re.IGNORECASE) for p in exclusions.get("stock_only_patterns", [])]
    # Back-compat: keep the old single regex as a fallback
    if not stock_patterns:
        stock_patterns = [re.compile(exclusions.get("title_regex_stock_only", _STOCK_PATTERN_DEFAULT.pattern), re.IGNORECASE)]

    # Topics that override stock-noise filter (these are genuinely material even if stock-y)
    OVERRIDE_TOPICS = {"financial_distress_or_crisis", "ma_and_corporate_activity", "regulatory_and_compliance", "leadership_and_governance"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    survivors: list[NewsItem] = []
    dropped = {"length": 0, "lang": 0, "age": 0, "sponsored": 0, "stock_only": 0, "domain": 0, "drop_phrase": 0, "stock_domain": 0}

    for item in items:
        # Domain blocklist
        if item.source_domain in blocked_domains:
            dropped["domain"] += 1
            continue
        # Sponsored / advertorial URL markers
        if any(pat in item.url.lower() for pat in url_patterns):
            dropped["sponsored"] += 1
            continue
        # Drop-phrase titles
        title_lower = item.title.lower()
        if any(phrase.lower() in title_lower for phrase in drop_phrases):
            dropped["drop_phrase"] += 1
            continue
        # Stock-noise domain (drop unless the article looks materially newsworthy)
        # At this point matched_topics isn't populated yet — scoring runs after this stage.
        # So we use a cheap content-based check: does the title mention any override-worthy term?
        override_terms = [
            # Corporate events
            "acquisition", "merger", "takeover", "profit warning", "special administration",
            "downgrade", "upgrade", "fine", "enforcement", "investigation", "results",
            "equity raise", "rights issue", "rescue", "dividend cut", "dividend suspended",
            # Broker actions that ARE material (sector signal)
            "cuts target", "cuts price target", "raises target", "raises price target",
            "broker ratings", "citi cuts", "jpmorgan cuts", "barclays cuts",
            "downgrades", "upgrades",
            # Leadership
            "resign", "appoints", "CEO", "CFO", "chair", "chairman", "steps down",
        ]
        title_has_override = any(t.lower() in title_lower for t in override_terms)
        if item.source_domain in stock_noise_domains and not title_has_override:
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
