"""
v8 regression test — verify materiality gate, per-client cap, and concept-based clustering.

The previous problem: 9 SE Water articles, 7 UU, 11 Vodafone, with junior India hire,
Euro 2028 sponsorship, "expect delays roadworks", smartphone launches, and partner awards
all scoring as RELEVANT.

v8 fixes:
  - Materiality gate: items need a MATERIAL_TOPICS hit, high-cred source, or substantial £/$ figure
    to qualify for PRIORITY/RELEVANT. Pure client mentions in low-cred sources stay MENTIONED.
  - Non-material patterns block tier promotion: junior overseas hires, sponsorship deals,
    smartphone launches, partner awards, roadworks delays etc. → capped at MENTIONED
  - Concept clustering: leadership-departure synonyms ("quits / resigns / steps down /
    forced to resign") cluster as same story even when fuzzy title score is low
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from tracker import sources, filters, scoring, dedupe

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def load(name):
    return json.load(open(CONFIG / name))


def make(title, summary=None, source="Test", domain="example.com", days_ago=1):
    return sources.NewsItem(
        title=title,
        url=f"https://{domain}/article-{abs(hash(title)) % 1000000}",
        summary=summary or title,
        source_name=source,
        source_domain=domain,
        feed_tags=[],
        published_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


# (title, summary, source, domain, expected_tier, note)
MATERIALITY_CASES = [
    # ---- These should be RELEVANT or PRIORITY (have material signal) ----
    ("Thames Water enters special administration as creditors fail to agree", None,
     "Reuters", "reuters.com", ("PRIORITY", "RELEVANT"),
     "Special admin from Reuters — should be PRIORITY or RELEVANT (material)"),
    ("United Utilities to pump £230m into Wigan, Skelmersdale", None,
     "Google News", "news.google.com", ("RELEVANT",),
     "£230m investment figure should grant material signal"),
    ("Vodafone takes full ownership of mobile joint venture with Three for £4.3bn", None,
     "Google News", "news.google.com", ("RELEVANT",),
     "£4.3bn M&A figure should grant material signal"),
    ("Yorkshire Water pumped sewage into bathing waters for almost 8,000 hours in 2025", None,
     "Google News", "news.google.com", ("RELEVANT",),
     "Sewage pollution = customer_and_service material topic"),
    ("South East Water boss quits after supply failures", None,
     "BBC Business", "bbc.co.uk", ("RELEVANT", "PRIORITY"),
     "Leadership change + supply failures = material"),

    # Non-material: MENTIONED or FILTERED_OUT both acceptable (FILTERED_OUT is even better)
    ("Rashi Bhatla Chatrath Joins BT Group as Head of People & Culture, India", None,
     "Google News", "news.google.com", ("MENTIONED", "FILTERED_OUT"),
     "Junior overseas hire — non-material"),
    ("BT Group connects to Euro 2028 as telecoms partner", None,
     "Google News", "news.google.com", ("MENTIONED", "FILTERED_OUT"),
     "Sports sponsorship — non-material"),
    ("Vodafone UK now offering Motorola Razr Fold smartphone", None,
     "Google News", "news.google.com", ("MENTIONED", "FILTERED_OUT"),
     "Smartphone product launch — non-material"),
    ("Onecom lands triple win at Vodafone Partner Awards", None,
     "Google News", "news.google.com", ("MENTIONED", "FILTERED_OUT"),
     "Partner Awards — non-material"),
    ("Yorkshire Water warn motorists to 'expect delays' on Skipton Road from Monday", None,
     "Google News", "news.google.com", ("MENTIONED", "FILTERED_OUT"),
     "Roadworks — non-material"),
    ("Severn Trent seeks to grub out 70-foot hedge", None,
     "Local Paper", "punchline-gloucester.example", ("MENTIONED", "FILTERED_OUT"),
     "Hedge removal — non-material"),
]


def run_materiality_tests():
    clients = load("clients.json")
    topics = load("relevance_topics.json")
    credibility = load("source_credibility.json")
    exclusions = load("exclusions.json")

    passed = failed = 0
    for title, summary, source, domain, expected_tiers, note in MATERIALITY_CASES:
        item = make(title, summary, source, domain)
        detected = filters.detect_clients([item], clients)
        quality = filters.quality_gate(detected, exclusions)
        if not quality:
            actual_tier = "FILTERED_OUT"
        else:
            scoring.score_and_tier(quality, topics, credibility, exclusions)
            actual_tier = quality[0].tier

        if actual_tier in expected_tiers:
            print(f"  ✓ {note}: {actual_tier}")
            passed += 1
        else:
            print(f"  ✗ FAIL {note}")
            print(f"      title:        {title[:80]}")
            print(f"      expected:     one of {expected_tiers}")
            print(f"      actual:       {actual_tier}")
            failed += 1
    print(f"\nMateriality gate: {passed} passed, {failed} failed")
    return failed == 0


def run_concept_clustering_test():
    print("\n--- Concept-based clustering ---")
    clients = load("clients.json")
    topics = load("relevance_topics.json")
    credibility = load("source_credibility.json")
    exclusions = load("exclusions.json")

    # 5 leadership-departure variants with low pairwise fuzz score
    titles = [
        "South East Water boss quits after supply failures",
        "CEO of South East Water resigns after mounting calls",
        "South East Water chief executive steps down following MPs report",
        "Why South East Water's CEO has been forced to resign",
        "South East Water announces resignation of David Hinton as Chief Executive",
    ]
    items = [make(t, source="Google News", domain="news.google.com", days_ago=2) for t in titles]
    items = filters.detect_clients(items, clients)
    items = filters.quality_gate(items, exclusions)
    scoring.score_and_tier(items, topics, credibility, exclusions)
    clustered = dedupe.cluster_by_story(items, credibility)

    visible_se = [it for it in clustered if "South East Water" in it.matched_clients and it.tier in ("PRIORITY", "RELEVANT", "MENTIONED")]
    print(f"  Input: {len(items)} leadership-departure articles (all SE Water)")
    print(f"  After clustering: {len(clustered)} items, of which {len(visible_se)} are SE Water visible")
    if len(visible_se) <= 2:
        print(f"  ✓ Concept clustering merged leadership-departure variants (≤2 visible)")
        return True
    else:
        print(f"  ✗ FAIL: expected ≤2 visible SE Water items, got {len(visible_se)}")
        return False


if __name__ == "__main__":
    ok_a = run_materiality_tests()
    ok_b = run_concept_clustering_test()
    print("\nAll v8 tests passed." if (ok_a and ok_b) else "\nSome tests failed.")
