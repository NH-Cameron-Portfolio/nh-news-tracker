"""
v9d regression test — recalibration of the materiality gate for MEC + two bug fixes.
Also re-locks the v9c suffix-credibility behaviour (lost in a bad bundle).

Fixes covered:
  1. Daily price-movement / stock chatter is FILTERED ("slides Tuesday", "ex-div",
     "FTSE fallers", "deserve a spot on your watchlist", "Expert View", "newspaper preview").
  2. Forward-looking analyst WARNINGS still survive (dividend warning, credit downgrade,
     going-concern) — these are material.
  3. Senior-leadership strategy interviews now surface ("CEO on why...", "chief executive tells").
  4. MOSL no longer matches the Indian financial firm Motilal Oswal (ICICI/SBI/ET Now noise).
  5. Vodafone foreign content "Fastweb + Vodafone" (spaced) no longer matches.
  6. v9c: FT/Times suffix gives credibility weight so high-cred outlet wins dedup.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tracker import sources, filters, scoring, dedupe

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def load(name):
    return json.load(open(CONFIG / name))


def mk(title, summary=None, source="Google News", domain="news.google.com", days_ago=2):
    return sources.NewsItem(
        title=title, url=f"https://{domain}/{abs(hash(title)) % 99999}",
        summary=summary or title, source_name=source, source_domain=domain,
        feed_tags=[], published_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


def tier_of(item, clients, topics, credibility, exclusions):
    d = filters.detect_clients([item], clients)
    if not d:
        return "NO-MATCH"
    q = filters.quality_gate(d, exclusions)
    if not q:
        return "FILTERED"
    scoring.score_and_tier(q, topics, credibility, exclusions)
    return q[0].tier


def run():
    clients = load("clients.json")
    topics = load("relevance_topics.json")
    credibility = load("source_credibility.json")
    exclusions = load("exclusions.json")

    passed = failed = 0

    def expect(title, predicate, note, summary=None):
        nonlocal passed, failed
        item = mk(title, summary)
        result = tier_of(item, clients, topics, credibility, exclusions)
        ok = predicate(result)
        if ok:
            print(f"  ✓ {note}: {result}")
            passed += 1
        else:
            print(f"  ✗ FAIL {note}: got {result}")
            print(f"      title: {title[:70]}")
            failed += 1

    FILTERED = lambda r: r in ("FILTERED", "NO-MATCH")
    SURVIVES = lambda r: r in ("RELEVANT", "PRIORITY")
    MATCHES = lambda r: r not in ("NO-MATCH", "FILTERED")

    print("--- 1. Price-movement chatter should be FILTERED ---")
    expect("Vodafone and Sainsbury's lead FTSE 100 fallers as shares trade ex-div - Proactive Investors", FILTERED, "FTSE fallers ex-div")
    expect("United Utilities Group slides Tuesday, underperforms market - MarketWatch", FILTERED, "slides Tuesday")
    expect("Does United Utilities Group (LON:UU.) Deserve A Spot On Your Watchlist? - simplywall.st", FILTERED, "watchlist chatter")
    expect("The Expert View: BT Group, Merlin Entertainments & Bellway - Citywire", FILTERED, "Expert View")
    expect("Informa plc (IFJPY) Stock Price, News, Quote & History - Yahoo Finance", FILTERED, "stock quote page")
    expect("Wednesday newspaper preview: South West Water, Hyve, Royal Exchange - Sharecast.com", FILTERED, "newspaper preview")

    print("\n--- 2. Forward-looking analyst warnings should SURVIVE ---")
    expect("BT dividend growth may be limited by debt targets as gilt yields rise, warns UBS - Yahoo Finance UK", SURVIVES, "UBS dividend warning")
    expect("Severn Trent credit rating downgraded by Moody's on debt concerns - Reuters", SURVIVES, "credit downgrade")
    expect("Thames Water faces going concern warning from auditors - Financial Times", SURVIVES, "going-concern warning")

    print("\n--- 3. Senior-leadership strategy interviews should SURFACE ---")
    expect('BT CEO on why telecoms needs "more strikers" - Fortune', SURVIVES, "BT CEO interview")
    expect("Severn Trent chief executive tells investors of turnaround plan - Reuters", SURVIVES, "chief exec interview")

    print("\n--- 4. MOSL (Indian financial firm) should NOT match ---")
    expect("ICICI Bank share price in focus: MOSL maintains buy rating on bank stock - ET Now", FILTERED, "Indian MOSL/ICICI")
    expect("ITC shares near 52-week lows; Here's why MOSL is cautious - CNBC TV18", FILTERED, "Indian MOSL/ITC")
    expect("SBI Share in Focus: MOSL sees over 34% upside - ET Now", FILTERED, "Indian MOSL/SBI")

    print("\n--- 4b. MOSL (real water market operator) SHOULD match ---")
    expect("MOSL confirms new settlement process for non-household water retail market - Utility Week", MATCHES, "real water MOSL")
    expect("Market Operator Services Limited updates CMOS switching rules - Water Magazine", MATCHES, "MOSL full name")

    print("\n--- 5. Fastweb + Vodafone (Italy) should NOT match ---")
    expect("What's up with... AI factories in France, Nvidia, Fastweb + Vodafone - telecomtv.com", FILTERED, "Fastweb+Vodafone spaced")
    expect("Italy's antitrust to probe Fastweb+Vodafone, TIM 5G network sharing deal - Telecompaper", FILTERED, "Fastweb+Vodafone unspaced")

    print("\n--- 5b. Vodafone UK SHOULD still match ---")
    expect("Vodafone UK launches new business division - Reuters", MATCHES, "Vodafone UK")
    expect("Vodafone Three merger gets Ofcom approval - Financial Times", MATCHES, "Vodafone Three merger")

    print("\n--- 6. v9c: FT/Times suffix wins dedup over tabloid ---")
    sun = mk("Thames Water draws up rescue plan as creditors circle - The Sun", days_ago=1)
    ft = mk("Thames Water draws up rescue plan as creditors circle - Financial Times", days_ago=2)
    items = filters.detect_clients([sun, ft], clients)
    items = filters.quality_gate(items, exclusions)
    items = scoring.score_and_tier(items, topics, credibility, exclusions)
    deduped = dedupe.deduplicate(items, credibility)
    if len(deduped) == 1 and "financial times" in deduped[0].title.lower():
        print(f"  ✓ FT beat The Sun in dedup")
        passed += 1
    else:
        print(f"  ✗ FAIL: FT did not win dedup ({len(deduped)} items)")
        failed += 1

    print(f"\nv9d: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run()
    print("\nAll v9d tests passed." if ok else "\nSome tests failed.")
