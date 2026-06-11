"""
cameron_feed.py — additional output step for the Cameron Portfolio microsite.

Writes a flat JSON array (cameron_news.json) to the repo root each run, alongside the
existing CSV/HTML/email outputs. This is a pure byproduct: it reads the same final
items the digest uses and does not change anything about the email or its recipients.

The microsite fetches the raw GitHub URL of cameron_news.json client-side and renders
news cards. If the fetch fails it falls back to hardcoded content, so a malformed or
missing file can never break the microsite — but we still write valid JSON every time.

Schema (one object per news item):
    industry     : one of exactly "energy" | "utilities" | "media" | "comms"
    period       : human-readable label, e.g. "Week of 9 Jun 2026"
    source       : publication / organisation, e.g. "FT", "Ofwat", "Reuters"
    headline     : plain-text headline
    body         : plain-text 2-5 sentence summary
    why_matters  : plain-text "so what for NH" angle, or "" if not produced
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

from tracker.sources import NewsItem

log = logging.getLogger(__name__)


# Map the bot's internal sectors to the four microsite industry values.
# Judgement calls:
#   - Water                -> utilities (clearly a utility)
#   - Electricity, Gas     -> energy   (generation/supply/networks all sit under "energy" here)
#   - Telecoms             -> comms
#   - Media                -> media
#   - Regulator / Industry Body -> routed by the specific body (see _industry_for_item),
#         because e.g. Ofwat is utilities, Ofgem is energy, Ofcom-adjacent bodies are comms.
_SECTOR_TO_INDUSTRY = {
    "Water": "utilities",
    "Electricity": "energy",
    "Gas": "energy",
    "Telecoms": "comms",
    "Media": "media",
}

# Regulators / industry bodies don't have an intrinsic energy-vs-utilities answer from their
# sector label alone, so map them by canonical client name.
_CLIENT_NAME_TO_INDUSTRY = {
    # Water regulators / bodies
    "Ofwat": "utilities",
    "CCW": "utilities",
    "Water UK": "utilities",
    "MOSL": "utilities",
    "RECCo": "utilities",
    # Energy regulators / bodies
    "Ofgem": "energy",
    "Energy UK": "energy",
    "Elexon": "energy",
    "Xoserve": "energy",
    "Energy Networks Association": "energy",
    "Future Energy Networks": "energy",
    "Climate Change Committee": "energy",
    "Gemserv": "energy",
    # Comms
    "Ofcom": "comms",
}

_DEFAULT_INDUSTRY = "energy"  # safest catch-all for unmapped energy/utilities bodies


def _industry_for_item(item: NewsItem, sector_map: dict[str, str]) -> str:
    """Resolve an item to one of the four microsite industry values."""
    # 1. If any matched client has an explicit name-based mapping, prefer it.
    for client in item.matched_clients:
        if client in _CLIENT_NAME_TO_INDUSTRY:
            return _CLIENT_NAME_TO_INDUSTRY[client]
    # 2. Otherwise use the primary client's sector.
    for client in item.matched_clients:
        sector = sector_map.get(client)
        if sector in _SECTOR_TO_INDUSTRY:
            return _SECTOR_TO_INDUSTRY[sector]
    # 3. Fallback.
    return _DEFAULT_INDUSTRY


def _clean_text(s: str) -> str:
    """Strip markdown-ish artefacts and collapse whitespace to plain text."""
    if not s:
        return ""
    # Remove the Google-News '- Source' suffix if present in a title
    s = re.sub(r"\s+[-|–]\s+[^-|–]{2,60}$", "", s)
    # Strip common markdown markers and collapse whitespace
    s = re.sub(r"[*_`#>]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _source_label(item: NewsItem) -> str:
    """Human-readable source. Prefer the real outlet from the Google-News title suffix."""
    m = re.search(r"\s+[-|–]\s+([^-|–]{2,60})$", item.title or "")
    if m:
        return m.group(1).strip()
    # Fall back to the feed/source name, stripping the "Google News: X" prefix
    name = item.source_name or ""
    name = re.sub(r"^Google News:\s*", "", name).strip()
    return name or "Unknown"


def _period_label(run_date: date) -> str:
    """Human-readable period label, matching the digest's 'Week of' convention."""
    # Match the email's date format, e.g. "Week of 9 Jun 2026"
    try:
        day = run_date.strftime("%-d")       # no leading zero (Linux)
    except ValueError:
        day = run_date.strftime("%d").lstrip("0")
    return f"Week of {day} {run_date.strftime('%b %Y')}"


def build_feed_items(items: list[NewsItem], run_date: date, sector_map: dict[str, str]) -> list[dict]:
    """Convert final digest items into the microsite's flat schema."""
    period = _period_label(run_date)
    feed: list[dict] = []
    for it in items:
        feed.append({
            "industry": _industry_for_item(it, sector_map),
            "period": period,
            "source": _source_label(it),
            "headline": _clean_text(it.title),
            "body": _clean_text(it.summary)[:600],
            "why_matters": _clean_text(it.why_it_matters),
        })
    return feed


def write_cameron_json(
    items: list[NewsItem],
    run_date: date,
    sector_map: dict[str, str],
    out_path: Path,
) -> Path:
    """
    Write cameron_news.json (overwriting any previous version).

    `items` should be the final, non-DISCARDED items (PRIORITY/RELEVANT, plus MENTIONED
    if the digest includes them) — i.e. the same set the email shows.
    """
    feed = build_feed_items(items, run_date, sector_map)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(feed, indent=2, ensure_ascii=False), encoding="utf-8")
    # Log the per-industry breakdown so it's visible in the Actions log
    from collections import Counter
    breakdown = dict(Counter(f["industry"] for f in feed))
    log.info("Wrote %s with %d items %s", out_path.name, len(feed), breakdown)
    return out_path
