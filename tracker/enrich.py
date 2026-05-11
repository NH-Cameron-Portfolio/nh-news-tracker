"""
enrich.py — Stage 6 (optional): Claude API pass to add "why it matters" and final relevance check.

Off by default. Enable by setting env var ENABLE_LLM_ENRICHMENT=1 and providing ANTHROPIC_API_KEY.

Designed to be cheap: uses Claude Haiku, batches items, only processes PRIORITY + RELEVANT (not MENTIONED).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from tracker.sources import NewsItem

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You are a relevance analyst for North Highland, a management consultancy serving UK regulated utilities (water, electricity, gas networks, regulators, and industry bodies).

For each news article you are given, you produce a strict JSON object with three fields:

- "keep": true if the article describes a material event likely to interest a consultancy partner (transformation, restructuring, M&A, regulatory action, leadership change, financial distress, major tech programmes, crisis events). False if it is fluff, routine PR, sport/celebrity tie-in, or a thin mention with no consulting angle.

- "why_it_matters": ONE short sentence (max 25 words) explaining the significance. Derive strictly from the supplied title and summary — do not invent facts. If the article does not clearly indicate significance, write exactly "Unclear from summary."

- "angle_for_nh": ONE short sentence (max 20 words) naming the most plausible consulting opportunity hook (e.g. "transformation programme entry point", "regulatory crisis advisory window", "post-deal integration", "leadership-change RFP risk"). If no plausible hook, write "None obvious."

You output ONLY a JSON object. No preamble, no markdown fences."""


def _build_user_message(item: NewsItem) -> str:
    return json.dumps({
        "title": item.title,
        "summary": item.summary[:1500],   # keep prompt size bounded
        "client": item.matched_clients,
        "source": item.source_name,
        "topics": item.matched_topics,
    })


def _call_claude(client: Any, item: NewsItem) -> dict | None:
    """One API call. Returns parsed JSON dict or None on failure."""
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_message(item)}],
        )
        text = resp.content[0].text.strip()
        # Strip code fences if the model added them despite instructions
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
    except Exception as exc:
        log.warning("Claude enrichment failed for '%s': %s", item.title[:60], exc)
        return None


def enrich_items(items: list[NewsItem]) -> list[NewsItem]:
    """
    Enrich PRIORITY and RELEVANT items with why_it_matters strings.
    Items with keep=false are demoted to DISCARDED tier.
    Returns the list with mutations applied.
    """
    if os.environ.get("ENABLE_LLM_ENRICHMENT", "").lower() not in ("1", "true", "yes"):
        log.info("LLM enrichment disabled (ENABLE_LLM_ENRICHMENT not set)")
        return items
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ENABLE_LLM_ENRICHMENT set but ANTHROPIC_API_KEY missing — skipping")
        return items

    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed — skipping enrichment")
        return items

    client = anthropic.Anthropic(api_key=api_key)
    targets = [it for it in items if it.tier in ("PRIORITY", "RELEVANT")]
    log.info("Enriching %d items with Claude (%s)", len(targets), MODEL)

    for item in targets:
        result = _call_claude(client, item)
        if not result:
            continue
        if not result.get("keep", True):
            item.tier = "DISCARDED"
            log.info("LLM demoted to DISCARDED: %s", item.title[:80])
            continue
        why = (result.get("why_it_matters") or "").strip()
        angle = (result.get("angle_for_nh") or "").strip()
        if angle and angle.lower() != "none obvious.":
            item.why_it_matters = f"{why} — {angle}"
        else:
            item.why_it_matters = why

    return items
