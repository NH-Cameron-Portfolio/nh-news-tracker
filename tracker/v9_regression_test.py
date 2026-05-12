"""
v9 regression test — verify that articles matching a client via a SHORT ALIAS
(not the canonical name) actually get scored. This was a long-standing bug:

  Article: "'BT is back' as telecoms group revives its consumer brand - FT"
  → Detection: matched 'BT Group' via alias 'BT'
  → Scoring (pre-v9): looked for 'BT Group' in title — not present → score 0 → DISCARDED
  → Scoring (v9): looks for 'BT' in title — present → score 5+ → RELEVANT

Same bug affected: Openreach articles (canonical='BT Group'), NESO articles
(canonical='National Grid'), TWUL articles (canonical='Thames Water'), etc.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tracker import sources, filters, scoring

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def load(name):
    return json.load(open(CONFIG / name))


def make(title, summary, source, domain):
    return sources.NewsItem(
        title=title,
        url=f"https://{domain}/article-{abs(hash(title))%99999}",
        summary=summary,
        source_name=source,
        source_domain=domain,
        feed_tags=[],
        published_at=datetime.now(timezone.utc) - timedelta(days=1),
    )


# Cases where the article uses a SHORT ALIAS but client canonical is different
ALIAS_SCORING_CASES = [
    ("'BT is back' as telecoms group revives its consumer brand - Financial Times",
     "'BT is back' as telecoms group revives its consumer brand Financial Times",
     "Google News", "news.google.com",
     "BT Group", "BT",
     "RELEVANT_OR_PRIORITY", "BT alias for BT Group"),
    ("UK's BT re-focuses on its core brand, with BT Mobile for consumers - Reuters",
     "UK's BT re-focuses on its core brand, with BT Mobile for consumers Reuters",
     "Google News", "news.google.com",
     "BT Group", "BT",
     "RELEVANT_OR_PRIORITY", "BT alias from Reuters"),
    ("Openreach announces FTTP rollout target met early",
     "Openreach has announced FTTP rollout exceeded its annual target.",
     "Reuters", "reuters.com",
     "BT Group", "Openreach",
     "RELEVANT_OR_PRIORITY", "Openreach as exact alias for BT Group"),
    ("NESO appoints new CDO to lead digital transformation",
     "The National Energy System Operator has appointed a new Chief Digital Officer.",
     "Utility Week", "utilityweek.co.uk",
     "National Grid", "NESO",
     "RELEVANT_OR_PRIORITY", "NESO as exact alias for National Grid"),
    ("TWUL placed in special administration as creditors fail to agree",
     "Thames Water Utilities Limited has been placed in special administration.",
     "Reuters", "reuters.com",
     "Thames Water", "TWUL",
     "RELEVANT_OR_PRIORITY", "TWUL as exact alias for Thames Water"),
]


def run():
    clients = load("clients.json")
    topics = load("relevance_topics.json")
    credibility = load("source_credibility.json")
    exclusions = load("exclusions.json")

    passed = failed = 0
    for title, summary, source, domain, expected_canonical, expected_alias, expected_tier_class, note in ALIAS_SCORING_CASES:
        item = make(title, summary, source, domain)
        items = filters.detect_clients([item], clients)
        items = filters.quality_gate(items, exclusions)
        if not items:
            print(f"  ✗ FAIL {note}: dropped at quality gate")
            failed += 1
            continue
        scoring.score_and_tier(items, topics, credibility, exclusions)
        it = items[0]

        # Check matched_aliases includes the expected alias
        alias_ok = expected_alias in it.matched_aliases
        # Check canonical matched
        canonical_ok = expected_canonical in it.matched_clients
        # Check tier is RELEVANT or PRIORITY (not MENTIONED or DISCARDED)
        tier_ok = it.tier in ("RELEVANT", "PRIORITY")
        # Check score > 0
        score_ok = it.score > 0

        if alias_ok and canonical_ok and tier_ok and score_ok:
            print(f"  ✓ {note}: tier={it.tier}, score={it.score}, alias={it.matched_aliases}")
            passed += 1
        else:
            print(f"  ✗ FAIL {note}")
            print(f"      title:     {title[:75]}")
            print(f"      expected:  canonical={expected_canonical}, alias contains {expected_alias}, tier RELEVANT/PRIORITY, score>0")
            print(f"      actual:    canonical={it.matched_clients}, aliases={it.matched_aliases}, tier={it.tier}, score={it.score}")
            failed += 1

    print(f"\nv9 alias scoring: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run()
    print("\nAll v9 tests passed." if ok else "\nSome tests failed.")
