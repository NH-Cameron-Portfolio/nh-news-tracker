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
    date         : ISO date YYYY-MM-DD (article publish date; run date if unknown)
    source       : publication / organisation, e.g. "FT", "Ofwat", "Reuters"
    headline     : plain-text headline
    body         : plain-text 2-5 sentence summary
    why_matters  : plain-text "so what for NH" angle, or "" if not produced
    url          : direct link to the source article (omitted only if genuinely absent)
    relevance    : integer 0-100; higher = more relevant. Maps the bot's internal
                   tier+score onto High(75-100)/Medium(40-74)/Low(0-39) bands.
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


# ---------- Relevance score (0-100) for the microsite ----------
# The microsite sorts each industry's items by `relevance` descending, shows the top 5,
# and hides anything below a threshold it sets. We map the bot's internal tier + raw score
# onto the 0-100 scale, aligned to the bands the microsite asked for:
#     High   75-100 : PRIORITY  (named clients, M&A, determinations, restructuring, big capex)
#     Medium 40-74  : RELEVANT  (sector-relevant, consultations, competitor moves)
#     Low    0-39   : MENTIONED (general noise, routine movements, tangential mentions)
# Within each band we spread by the raw score so finer ordering is preserved.

_TIER_BANDS = {
    "PRIORITY":  (75, 100),
    "RELEVANT":  (40, 74),
    "MENTIONED": (10, 39),
    "DISCARDED": (0, 9),
}

# Raw-score anchors: a PRIORITY item scores >=10; RELEVANT 5-9; MENTIONED 2-4.
# We linearly position the raw score within its tier's band, capping sensibly.
_TIER_SCORE_RANGE = {
    "PRIORITY":  (10, 22),   # 10 -> 75, 22+ -> 100
    "RELEVANT":  (5, 9),     # 5  -> 40, 9   -> 74
    "MENTIONED": (2, 4),     # 2  -> 10, 4   -> 39
    "DISCARDED": (0, 1),
}


def relevance_score(item: NewsItem) -> int:
    """Map the bot's internal tier + raw score onto a 0-100 relevance scale."""
    tier = item.tier or "MENTIONED"
    band_lo, band_hi = _TIER_BANDS.get(tier, (10, 39))
    score_lo, score_hi = _TIER_SCORE_RANGE.get(tier, (2, 4))
    raw = item.score
    if score_hi <= score_lo:
        frac = 1.0
    else:
        frac = (raw - score_lo) / (score_hi - score_lo)
        frac = max(0.0, min(1.0, frac))   # clamp to [0,1]
    return int(round(band_lo + frac * (band_hi - band_lo)))


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
        row = {
            "industry": _industry_for_item(it, sector_map),
            "period": period,
            "date": (it.published_at.date().isoformat() if it.published_at else run_date.isoformat()),
            "source": _source_label(it),
            "headline": _clean_text(it.title),
            "body": _clean_text(it.summary)[:600],
            "why_matters": _clean_text(it.why_it_matters),
            "relevance": relevance_score(it),
        }
        # url: include only when it's a real article link. Google-News RSS wraps links in
        # news.google.com redirect URLs which still resolve to the article, so they're usable.
        if it.url:
            row["url"] = it.url
        feed.append(row)
    # Sort by relevance desc, then date desc — so the file itself is already in priority order
    # (the microsite re-sorts, but this makes the raw file readable and a sensible fallback).
    feed.sort(key=lambda r: (r["relevance"], r.get("date", "")), reverse=True)
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
