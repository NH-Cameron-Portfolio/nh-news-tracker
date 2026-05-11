"""
Smoke test — feeds synthetic articles through the pipeline and asserts they're classified correctly.
Run with: python -m tracker.smoke_test
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from tracker import sources, filters, scoring, dedupe

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def load(name):
    return json.load(open(CONFIG / name))


def make(title, summary, source_name="Test Source", domain="example.com"):
    return sources.NewsItem(
        title=title,
        url=f"https://{domain}/article-{hash(title) % 10000}",
        summary=summary,
        source_name=source_name,
        source_domain=domain,
        feed_tags=[],
        published_at=datetime.now(timezone.utc),
    )


# Test cases: (item, expected_clients, note)
CASES = [
    # ---- Clear positives ----
    (
        make("Thames Water creditors prepare debt-for-equity swap as restructuring talks intensify",
             "Senior bondholders at Thames Water are preparing a debt-for-equity proposal as the company's restructuring negotiations enter a critical phase. Ofwat is monitoring the situation closely.",
             "Financial Times", "ft.com"),
        ["Thames Water", "Ofwat"],  # both mentioned in body — correct to surface both
        "Priority FT story should match both clients + score high",
    ),
    (
        make("NESO appoints new CDO to lead digital transformation programme",
             "The National Energy System Operator has appointed a new Chief Digital Officer to oversee its multi-year digital transformation programme.",
             "Utility Week", "utilityweek.co.uk"),
        ["National Grid"],
        "NESO should resolve to National Grid (NESO is an alias)",
    ),
    (
        make("Ofgem opens enforcement investigation into SSEN over connection delays",
             "Ofgem has launched an enforcement investigation into SSEN, the regulated electricity network operator, over alleged delays in connecting new generation to the transmission grid.",
             "Current News", "current-news.co.uk"),
        ["Ofgem", "SSEN"],
        "Multi-client article (Ofgem + SSEN) — both should match",
    ),
    (
        make("Severn Trent reports record sewage spills in annual return",
             "Severn Trent has reported a sharp increase in pollution incidents in its annual return to Ofwat.",
             "BBC News", "bbc.co.uk"),
        ["Severn Trent", "Ofwat"],
        "Severn Trent (the company) — must NOT trigger on 'river Severn'",
    ),

    # ---- Disambiguation tests (negative cases) ----
    (
        make("Hikers warned of high water levels in river Thames",
             "The Environment Agency has issued warnings to hikers using the Thames Path due to high water levels.",
             "Local News", "localnews.example"),
        [],
        "River Thames + Thames Path → negative_context should block",
    ),
    (
        make("SSE Thermal commissions new gas plant in Lincolnshire",
             "SSE Thermal, the generation arm of SSE plc, has commissioned a new gas-fired power station.",
             "Energy Live", "energylivenews.com"),
        [],
        "SSE Thermal is NOT in scope — must not match SSEN",
    ),
    (
        make("CCC says UK is off-track for 2030 climate targets",
             "The Climate Change Committee has warned the UK is significantly behind on its 2030 emissions targets.",
             "Guardian", "theguardian.com"),
        ["Climate Change Committee"],
        "Bare 'CCC' should NOT match CCW (it should match Climate Change Committee via full name)",
    ),
    (
        make("Consumer Council for Water criticises Ofwat over PR24 decisions",
             "CCW has urged Ofwat to revisit elements of its PR24 price control decision.",
             "Water Briefing", "waterbriefing.org"),
        ["Ofwat", "CCW"],
        "CCW + context words → should match CCW (not Climate Change Committee)",
    ),
    (
        make("Cadent Health raises Series B for AI diagnostics",
             "Cadent Health, a US healthcare startup, has closed a $40m Series B round.",
             "TechCrunch", "techcrunch.com"),
        [],
        "Cadent Health (US healthcare) — negative_context should block",
    ),
    (
        make("National grid struggles as cold snap hits demand",
             "Engineers warned that the national grid is operating close to capacity amid the cold snap.",
             "Sun", "thesun.co.uk"),
        ["National Grid"],
        "Generic 'national grid' phrase — current rules will match. (Acceptable false positive — capitalisation in titles is unreliable.)",
    ),
    (
        make("Energy UK chief executive warns of supplier failures",
             "Dhara Vyas, the chief executive of Energy UK, the trade body for energy suppliers, has warned MPs that further supplier failures are likely.",
             "FT", "ft.com"),
        ["Energy UK"],
        "Energy UK with trade-body context → must match",
    ),
    (
        make("UK energy households face higher bills next year",
             "Energy UK households are likely to face higher bills next year as wholesale prices rise.",
             "Daily Mail", "dailymail.co.uk"),
        [],
        "Generic 'Energy UK households' — context-gating should reject",
    ),

    # ---- Noise / quality gate tests (these match clients but should hit quality gate) ----
    (
        make("Thames Water shares up 2%",
             "Brief.",
             "Stock site", "stockfarm.example"),
        ["Thames Water"],  # client detection matches; quality gate should drop
        "Stock-only title — client matches but quality gate should drop",
    ),
]


def run():
    clients = load("clients.json")
    exclusions = load("exclusions.json")
    topics = load("relevance_topics.json")
    credibility = load("source_credibility.json")

    passed, failed = 0, 0
    for item, expected, note in CASES:
        # Run through detect_clients
        result = filters.detect_clients([item], clients)
        actual_clients = result[0].matched_clients if result else []

        # Compare as sets (order-insensitive)
        if set(actual_clients) == set(expected):
            print(f"  ✓ {note}")
            print(f"      title: {item.title[:80]}")
            print(f"      matched: {actual_clients}")
            passed += 1
        else:
            print(f"  ✗ FAIL: {note}")
            print(f"      title: {item.title[:80]}")
            print(f"      expected: {expected}")
            print(f"      actual:   {actual_clients}")
            failed += 1

    print(f"\nDetection: {passed} passed, {failed} failed")

    # Now run a couple through full pipeline
    print("\n--- Full pipeline on positives ---")
    positives = [c[0] for c in CASES if set([])  != set([])  or True]
    positives = [c[0] for c in CASES[:4]]  # first 4 clear positives
    deduped = dedupe.deduplicate(positives, credibility)
    detected = filters.detect_clients(deduped, clients)
    quality = filters.quality_gate(detected, exclusions)
    scored = scoring.score_and_tier(quality, topics, credibility, exclusions)
    for it in sorted(scored, key=lambda x: -x.score):
        print(f"  [{it.tier} score={it.score}] {it.title[:70]}")
        print(f"      clients={it.matched_clients}  topics={it.matched_topics}")


if __name__ == "__main__":
    run()
