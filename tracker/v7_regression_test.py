"""
v7 regression test — verify story clustering correctly collapses same-story coverage
while preserving distinct stories about the same client.

The original failure: 5 articles about South East Water CEO resignation appeared
separately in the digest instead of clustering. This was because the bucket key
included primary_topic, and different headlines tagged different primary topics
(e.g. 'CEO resigns' could be leadership_and_governance OR customer_and_service).

v7 fix: bucket by (client, week) and use fuzzy title clustering within each bucket.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from tracker import sources, filters, scoring, dedupe

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def load(name):
    return json.load(open(CONFIG / name))


def make(title, source, domain, days_ago):
    return sources.NewsItem(
        title=title,
        url=f"https://{domain}/article-{abs(hash(title)) % 1000000}",
        summary=title,
        source_name=source,
        source_domain=domain,
        feed_tags=[],
        published_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


def run():
    clients = load("clients.json")
    topics = load("relevance_topics.json")
    credibility = load("source_credibility.json")
    exclusions = load("exclusions.json")

    # The actual 5 South East Water articles from a real digest
    se_water = [
        make("South East Water boss quits after supply failures", "BBC", "bbc.co.uk", 3),
        make("South East Water boss quits a week after chairman", "Sky", "news.sky.com", 3),
        make("South East Water CEO to step down after Kent and Sussex supply outages - Guardian", "Google News", "news.google.com", 2),
        make("South East Water boss finally resigns after Tunbridge Wells taps run dry - Telegraph", "Google News", "news.google.com", 3),
        make("What next for South East Water after boss quits? - BBC", "Google News", "news.google.com", 2),
    ]
    # Three genuinely distinct NI Water stories
    ni_water = [
        make("NI Water responsible for 11% of water pollution incidents - Irish News", "Google News", "news.google.com", 0),
        make("NI Water Tap Refillution at Balmoral Show - Farming Life", "Google News", "news.google.com", 0),
        make("NI Water opens applications for Farming for Water scheme - NorthernIrelandWorld", "Google News", "news.google.com", 3),
    ]

    all_items = se_water + ni_water
    all_items = filters.detect_clients(all_items, clients)
    all_items = filters.quality_gate(all_items, exclusions)
    all_items = scoring.score_and_tier(all_items, topics, credibility, exclusions)
    clustered = dedupe.cluster_by_story(all_items, credibility)

    se_count = sum(1 for it in clustered if "South East Water" in it.matched_clients)
    ni_count = sum(1 for it in clustered if "NI Water" in it.matched_clients)

    passed = failed = 0

    if se_count == 1:
        print(f"  ✓ 5 South East Water CEO-resignation articles collapsed to 1")
        passed += 1
    else:
        print(f"  ✗ FAIL: expected 1 SE Water item after clustering, got {se_count}")
        failed += 1

    if ni_count == 3:
        print(f"  ✓ 3 distinct NI Water stories preserved (pollution, Refillution, Farming scheme)")
        passed += 1
    else:
        print(f"  ✗ FAIL: expected 3 distinct NI Water items, got {ni_count}")
        failed += 1

    # The SE Water winner should be annotated with "+N other outlets covered this"
    se_winner = next((it for it in clustered if "South East Water" in it.matched_clients), None)
    if se_winner and "other outlet" in (se_winner.why_it_matters or ""):
        print(f"  ✓ SE Water winner annotated: '{se_winner.why_it_matters}'")
        passed += 1
    else:
        print(f"  ✗ FAIL: SE Water winner missing 'other outlets' annotation. Got: '{se_winner.why_it_matters if se_winner else None}'")
        failed += 1

    print(f"\nv7 story clustering: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run()
    print("\nAll v7 tests passed." if ok else "\nSome tests failed.")
